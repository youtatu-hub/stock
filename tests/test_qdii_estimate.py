from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import app
from analysis.qdii_estimate import calculate_estimate
from data import qdii_estimate as data
from data.ai_client import SYSTEM_PROMPT
from data.instruments import get_instrument
from data.kline_fetcher import Quote
from data.us_stock_time import next_us_stock_close, us_stock_close
from tools import qdii_baselines


NAV_DAY = date(2026, 11, 20)
CLOSE = datetime(2026, 11, 20, 21, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 24, 2, tzinfo=timezone.utc)


def market_quotes(moment=NOW, *, nq=110, es=210, fx=7.14):
    values = {"NQ": nq, "ES": es, "USD/CNH": fx}
    return {symbol: {"symbol": symbol, "series": series, "price": values[symbol],
                     "price_basis": data.price_basis(symbol), "time": moment.isoformat(),
                     "source": "fixture", "source_url": "https://example.test/quote",
                     "change_pct": 1, "last_settle": 109}
            for symbol, series in data.MARKET_SYMBOLS.items()}


def baseline():
    return {"nav_date": NAV_DAY.isoformat(), "close_time": CLOSE.isoformat(), "origin": "manual",
            "source": "deterministic fixture", "quotes": market_quotes(CLOSE, nq=100, es=200, fx=7)}


def nav():
    return {"history": [{"date": NAV_DAY.isoformat(), "nav": 1}],
            "source": "fixture", "source_url": "https://example.test/nav", "fetched_at": NOW.isoformat()}


def quote():
    return {"price": 1.2, "time": NOW.isoformat(), "stale": False}


def quote_text():
    lines = []
    for key, price in (("hf_NQ", "110"), ("hf_ES", "210")):
        fields = [price, "99", "109", "111", "112", "108", "10:00:00", "109", "109", "0", "1", "1", "2026-11-24", "fixture", "0"]
        lines.append(f'var hq_str_{key}="{",".join(fields)}";')
    fx = ["10:00:00", "7.139", "7.141", "7.0", "0", "7", "7.2", "6.9", "9.99", "fixture", "99", "0", "0", "", "7", "7", "", "2026-11-24"]
    lines.append(f'var hq_str_fx_susdcnh="{",".join(fx)}";')
    return "\n".join(lines)


class EstimateCalculationTests(unittest.TestCase):
    def test_friday_nav_to_tuesday_uses_full_price_ratio_not_daily_change(self):
        result = calculate_estimate(nav()["history"][-1], baseline(), market_quotes(),
                                    future_symbol="NQ", quote=quote(), now=NOW)
        self.assertAlmostEqual(result["est_nav"], 1.122)
        self.assertAlmostEqual(result["est_premium_pct"], 6.952, places=3)
        self.assertEqual(result["nav_date"], "2026-11-20")
        self.assertEqual(result["kind"], "model_estimate")
        self.assertEqual(result["label"], "模型估算")
        self.assertTrue(result["notice"])
        self.assertFalse(result["alert_eligible"])
        self.assertNotIn("iopv", result)
        json.dumps(result, allow_nan=False)

    def test_fx_direction_and_price_discount(self):
        result = calculate_estimate(nav()["history"][-1], baseline(), market_quotes(fx=6.86),
                                    future_symbol="ES", quote={**quote(), "price": 1}, now=NOW)
        self.assertAlmostEqual(result["est_nav"], 1.029)
        self.assertLess(result["est_premium_pct"], 0)
        self.assertFalse(result["notice"])

    def test_old_future_invalid_rates_and_wrong_baseline_are_rejected(self):
        for offset in (-121, 1):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                calculate_estimate(nav()["history"][-1], baseline(), market_quotes(NOW + timedelta(seconds=offset)),
                                   future_symbol="NQ", now=NOW)
        wrong = baseline()
        wrong["nav_date"] = "2026-11-23"
        with self.assertRaisesRegex(ValueError, "日期"):
            calculate_estimate(nav()["history"][-1], wrong, market_quotes(), future_symbol="NQ", now=NOW)
        for bad in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                calculate_estimate(nav()["history"][-1], baseline(), market_quotes(fx=bad), future_symbol="NQ", now=NOW)

    def test_stale_etf_price_keeps_nav_but_not_estimated_premium(self):
        result = calculate_estimate(nav()["history"][-1], baseline(), market_quotes(), future_symbol="NQ",
                                    quote={**quote(), "stale": True}, now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["est_premium_pct"])
        self.assertIsNone(result["price"])
        self.assertFalse(result["notice"])

    def test_mixed_instrument_and_fx_basis_are_rejected(self):
        for symbol, key, value in (("NQ", "series", "other_contract"), ("USD/CNH", "price_basis", "central_parity")):
            raw = market_quotes()
            raw[symbol][key] = value
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "口径"):
                calculate_estimate(nav()["history"][-1], baseline(), raw, future_symbol="NQ", now=NOW)


class MarketAndCalendarTests(unittest.TestCase):
    def test_source_quotes_parse_prices_and_timestamps_not_percent_fields(self):
        parsed = data.parse_market_quotes(quote_text())
        self.assertEqual(parsed["NQ"]["price"], 110)
        self.assertAlmostEqual(parsed["USD/CNH"]["price"], 7.14)
        self.assertEqual(parsed["NQ"]["time"], "2026-11-24T10:00:00+08:00")
        self.assertNotIn("change_pct", parsed["NQ"])
        for text in ("", quote_text().replace("hf_ES", "hf_OTHER"), quote_text().replace("fx_susdcnh", "fx_other"),
                     quote_text().replace("7.139", "8.139")):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                data.parse_market_quotes(text)

    def test_failed_refresh_has_no_old_quote_fallback_or_success_cache(self):
        with patch.object(data, "_cache_get", return_value=None), \
                patch.object(data, "_http_get", side_effect=OSError("offline")), \
                patch.object(data, "_cache_set") as write, self.assertRaises(OSError):
            data.fetch_estimate_market()
        write.assert_not_called()

    def test_sina_batch_route_keeps_symbol_separators_unencoded(self):
        response = Mock(content=quote_text().encode("gb18030"))
        with patch.object(data, "_cache_get", return_value=None), \
                patch.object(data, "_cache_set"), patch.object(data, "_http_get", return_value=response) as fetch:
            result = data.fetch_estimate_market(force=True)
        self.assertEqual(set(result), {"NQ", "ES", "USD/CNH"})
        fetch.assert_called_once_with("https://hq.sinajs.cn/list=hf_NQ,hf_ES,fx_susdcnh",
                                      headers={"Referer": "https://finance.sina.com.cn/"})

    def test_dst_holidays_and_early_close(self):
        self.assertEqual(us_stock_close(NAV_DAY), CLOSE)
        summer = us_stock_close(date(2026, 7, 2)).astimezone(data.CHINA_TZ)
        self.assertEqual((summer.date(), summer.hour), (date(2026, 7, 3), 4))
        early = us_stock_close(date(2026, 11, 27)).astimezone(data.CHINA_TZ)
        self.assertEqual((early.date(), early.hour), (date(2026, 11, 28), 2))
        for day in (date(2026, 7, 3), date(2026, 11, 26), date(2026, 11, 21)):
            self.assertIsNone(us_stock_close(day))
        self.assertEqual(next_us_stock_close(CLOSE + timedelta(seconds=1)), datetime(2026, 11, 23, 21, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "2029"):
            us_stock_close(date(2029, 1, 2))


class BaselineAndApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = data.BaselineStore(Path(self.directory.name) / "baselines.sqlite3")

    def test_persistent_store_atomic_record_and_explicit_replace(self):
        self.assertIsNone(self.store.load(NAV_DAY.isoformat()))
        self.store.save(baseline())
        self.assertEqual(data.BaselineStore(self.store.path).load(NAV_DAY.isoformat())["quotes"]["NQ"]["price"], 100)
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            self.store.save(baseline())
        replacement = baseline()
        replacement["quotes"]["NQ"]["price"] = 101
        self.store.save(replacement, replace=True)
        self.assertEqual(self.store.load(NAV_DAY.isoformat())["quotes"]["NQ"]["price"], 101)
        self.assertEqual(self.store.dates(), ["2026-11-20"])

    def test_missing_or_misaligned_initial_baseline_is_not_filled(self):
        for change in ({"source": ""}, {"nav_date": 20261120}, {"quotes": {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.save({**baseline(), **change})
        bad = baseline()
        bad["quotes"]["NQ"]["time"] = (CLOSE + timedelta(seconds=31)).isoformat()
        with self.assertRaisesRegex(ValueError, "超过30"):
            self.store.save(bad)
        result = data.build_model_estimate(get_instrument("513300"), nav(), market_quotes(), store=self.store, now=NOW)
        self.assertEqual(result["reason_code"], "baseline_missing")
        self.assertIsNone(result["est_nav"])
        self.assertFalse(self.store.path.exists())

    def test_capture_writes_us_valuation_date_and_never_backfills_current_price(self):
        fetch = Mock(return_value=market_quotes(CLOSE + timedelta(seconds=2)))
        result = data.capture_close_baseline(self.store, fetcher=fetch, now=CLOSE + timedelta(seconds=5))
        self.assertEqual(result["nav_date"], "2026-11-20")
        self.assertEqual(self.store.load("2026-11-20")["origin"], "scheduled")
        fetch.reset_mock()
        self.assertEqual(data.capture_close_baseline(self.store, fetcher=fetch, now=CLOSE + timedelta(seconds=6))["status"], "exists")
        fetch.assert_not_called()
        with self.assertRaises(ValueError):
            data.capture_close_baseline(self.store, fetcher=fetch, now=NOW)
        fetch.assert_not_called()

    def test_capture_failure_and_late_response_do_not_write_any_baseline(self):
        with self.assertRaises(OSError):
            data.capture_close_baseline(self.store, fetcher=Mock(side_effect=OSError("offline")), now=CLOSE + timedelta(seconds=1))
        self.assertEqual(self.store.dates(), [])
        clock = Mock()
        clock.now.side_effect = [CLOSE + timedelta(seconds=1), CLOSE + timedelta(seconds=61)]
        with patch.object(data, "datetime", clock), self.assertRaisesRegex(ValueError, "超过"):
            data.capture_close_baseline(self.store, fetcher=Mock(return_value=market_quotes(CLOSE)))
        self.assertEqual(self.store.dates(), [])

    def test_five_etfs_use_exact_nav_day_and_correct_proxy(self):
        self.store.save(baseline())
        for symbol in ("513300", "513500", "513100", "159501", "159941"):
            result = data.build_model_estimate(get_instrument(symbol), nav(), market_quotes(), quote(), store=self.store, now=NOW)
            self.assertEqual(result["future_symbol"], "ES" if symbol == "513500" else "NQ")
            self.assertAlmostEqual(result["est_nav"], 1.071 if symbol == "513500" else 1.122)
        latest = nav()
        latest["history"].append({"date": "2026-11-23", "nav": 1.05})
        result = data.build_model_estimate(get_instrument("513300"), latest, market_quotes(), store=self.store, now=NOW)
        self.assertEqual((result["reason_code"], result["nav_date"]), ("baseline_missing", "2026-11-23"))

    def test_unavailable_fx_does_not_reuse_a_previous_estimate(self):
        self.store.save(baseline())
        good = data.build_model_estimate(get_instrument("513300"), nav(), market_quotes(), store=self.store, now=NOW)
        self.assertEqual(good["status"], "ok")
        stale = market_quotes()
        stale["USD/CNH"]["time"] = (NOW - timedelta(seconds=121)).isoformat()
        result = data.build_model_estimate(get_instrument("513300"), nav(), stale, store=self.store, now=NOW)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["est_nav"])
        self.assertIsNone(result["est_premium_pct"])
        self.assertFalse(result["alert_eligible"])

    def test_endpoint_keeps_estimate_separate_from_iopv(self):
        self.store.save(baseline())
        listed = Quote("513300", "fixture", 1.2, 0, 0, 1, 1, 1, 1, 1, 1, 1, timestamp=NOW.isoformat())
        with patch.object(app, "fetch_estimate_market", return_value=market_quotes()), \
                patch.object(app, "fetch_fund_nav", return_value=nav()), \
                patch.object(app, "fetch_quote", return_value=listed), \
                patch.object(app, "fetch_etf_premium", return_value={"status": "unavailable", "iopv": None, "premium_pct": None}), \
                patch.object(data, "baseline_store", self.store), \
                patch("analysis.qdii_estimate.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result = app.handle_etf_premium({"symbol": ["513300"]})
        self.assertAlmostEqual(result["model_estimate"]["est_nav"], 1.122)
        self.assertIsNone(result["iopv"]["iopv"])
        self.assertFalse(result["model_estimate"]["alert_eligible"])
        self.assertIn("不是官方IOPV", SYSTEM_PROMPT)

    def test_cli_template_stays_empty_until_verified_prices_are_supplied(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(qdii_baselines.main(["template", "--date", "2026-11-20"]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["close_time"], CLOSE.isoformat())
        self.assertTrue(all(value["price"] is None for value in record["quotes"].values()))
        with self.assertRaises(ValueError):
            self.store.save(record)


if __name__ == "__main__":
    unittest.main()
