from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import app
from analysis.asset_analysis import analyze_listed_fund, price_metrics
from data import etf_premium, fund_terms
from data.instruments import get_instrument, instrument_catalog
from data.kline_fetcher import Kline


def bars():
    start = date(2026, 1, 1)
    return [Kline(str(start + timedelta(days=i)), 10 + i / 100, 10 + i / 100,
                  10.1 + i / 100, 9.9 + i / 100, 1000) for i in range(100)]


def terms_html(symbol="050025", limit="---"):
    # 合成HTML保留来源的嵌套标签、表头和金额/期限分档，不保存真实行情。
    return f'''<title>测试基金({symbol})基金费率</title>
      <h4><label>交易状态</label><label></label></h4>
      <table class="w770 comm jjfl"><tr><td>申购状态</td><td>暂停申购</td>
        <td>赎回状态</td><td>开放赎回</td><td>定投状态</td><td>不支持</td></tr></table>
      <h4><label>申购与赎回金额</label></h4>
      <table class="comm jjfl"><tr><td>日累计申购限额</td><td>{limit}</td></tr></table>
      <h4><label>运作费用</label></h4>
      <table class="comm jjfl"><tr><td>销售服务费率</td><td>0.35%（每年）</td></tr></table>
      <h4><label>认购费率</label></h4>
      <table class="comm jjfl"><tr><td>小于100万元</td><td>9.00%</td></tr></table>
      <h4><label>申购费率</label></h4>
      <table class="comm jjfl"><thead><tr><th>适用金额</th><th>费率</th></tr></thead>
        <tr><td>小于100万元</td><td><strike>1.20%</strike>&nbsp; | &nbsp;0.12%</td></tr>
        <tr><td>大于等于100万元</td><td>每笔1000元</td></tr></table>
      <h4><label>赎回费率<a name="shfl"></a></label></h4>
      <table class="comm jjfl"><tr><td>小于7天</td><td>1.50%</td></tr>
        <tr><td>大于等于7天</td><td>0.00%</td></tr></table>'''


def premium_payload(**overrides):
    row = {"f12": "513500", "f14": "测试ETF", "f2": 1.1, "f441": 1,
           "f402": -10, "f297": 20260105,
           "f124": int(datetime(2026, 1, 5, 2, tzinfo=timezone.utc).timestamp())}
    return {"data": {"diff": [{**row, **overrides}]}}


class AssetProfileTests(unittest.TestCase):
    def test_catalog_pairs_currency_and_migration_versions(self):
        catalog = instrument_catalog()
        self.assertEqual(get_instrument("016055")["peer_code"], "016057")
        for code in ["006075", "015300", "016057", "006479", "014978"]:
            item = get_instrument(code)
            self.assertEqual((item["model"], item["share_class"], item["currency"]), ("nav", "C", "CNY"))
            self.assertEqual(get_instrument(item["peer_code"])["peer_code"], code)
            self.assertEqual(item["seed_version"], 3)
        self.assertEqual(len(catalog["instruments"]), 22)
        self.assertEqual(get_instrument("sh511360")["model"], "defensive")
        self.assertEqual(get_instrument("159999")["model"], "reference")
        self.assertEqual(get_instrument("600000")["model"], "stock")

    def test_bonds_skip_stock_engine_and_keep_missing_scores_null(self):
        for code in ["511360", "511090"]:
            with self.subTest(code=code), patch.object(app, "fetch_kline", return_value=bars()), \
                    patch.object(app, "fetch_quote", return_value=None), \
                    patch.object(app, "fetch_kline_extras", return_value={}), \
                    patch.object(app, "run_analysis") as stock_engine, \
                    patch.object(app, "fetch_index_kline") as index, \
                    patch.object(app, "_apply_signal_optimization") as policy:
                result = app.handle_analyze({"symbol": [code]})
                signal = result["signal"]
                self.assertIsNone(signal["score"])
                self.assertIsNone(signal["confidence"])
                self.assertIsNone(signal["canslim"])
                self.assertEqual(signal["breakouts"], [])
                self.assertEqual(signal["trade_plan"], {"executable": False})
                self.assertEqual(signal["input_snapshot"]["status"], "complete")
                stock_engine.assert_not_called()
                index.assert_not_called()
                policy.assert_not_called()
                json.dumps(result, allow_nan=False)

    def test_equity_and_overseas_keep_price_models_without_stock_plans(self):
        for code in ["512050", "513500", "518880"]:
            signal = analyze_listed_fund(bars(), get_instrument(code))
            self.assertEqual(signal.model, "technical")
            self.assertIsNotNone(signal.trend)
            self.assertTrue(signal.breakouts)
            self.assertEqual(signal.module_scores, {})
            self.assertFalse(signal.trade_plan["executable"])
            self.assertNotIn("买入", signal.action)

    def test_drawdown_uses_peak_and_selected_interval(self):
        points = bars()[:4]
        for row, close in zip(points, [10, 12, 9, 11]):
            row.close = close
        metrics = price_metrics(points)
        self.assertEqual(metrics["max_drawdown_pct"], -25)
        self.assertEqual(metrics["return_pct"], 10)

    def test_disallowed_models_do_not_fetch_inputs(self):
        with patch.object(app, "fetch_required") as fetch:
            self.assertIn("error", app.handle_analyze({"symbol": ["006075"]}))
            self.assertIn("error", app.handle_chanlun_daily({"symbol": ["511360"]}))
            self.assertIn("error", app.handle_chanlun_minute({"symbol": ["511090"]}))
            fetch.assert_not_called()


class FundTermsTests(unittest.TestCase):
    def test_status_is_separate_from_limit_and_missing_is_not_unlimited(self):
        result = fund_terms.parse_fund_terms(terms_html(), "050025")
        self.assertIsNone(result["trading"]["daily_limit"])
        result = fund_terms.parse_fund_terms(terms_html(limit="100.00元"), "050025")
        self.assertEqual(result["trading"]["subscription"], "暂停申购")
        self.assertEqual(result["trading"]["daily_limit"], "100.00元")
        self.assertEqual(result["fees"]["sales_service"], "0.35%（每年）")
        self.assertEqual(result["fees"]["subscription"][0]["rate"], "1.20% | 0.12%")
        self.assertEqual(result["fees"]["redemption"][-1]["rate"], "0.00%")
        self.assertNotIn("9.00%", str(result))

    def test_wrong_symbol_and_source_layout_change_are_errors(self):
        with self.assertRaisesRegex(ValueError, "代码"):
            fund_terms.parse_fund_terms(terms_html("006075"), "050025")
        with self.assertRaisesRegex(ValueError, "申赎"):
            fund_terms.parse_fund_terms("<title>测试(050025)</title>", "050025")

    def test_failed_peer_is_explicit_and_does_not_erase_selected_share(self):
        def fetch(code):
            if code == "006075":
                raise OSError("对照份额来源不可用")
            return fund_terms.parse_fund_terms(terms_html(), code)
        with patch.object(app, "fetch_fund_terms", side_effect=fetch):
            result = app.handle_fund_terms({"symbol": ["050025"]})
        self.assertEqual(result["status"], "partial")
        self.assertIsNotNone(result["items"][0]["data"])
        self.assertIsNone(result["items"][1]["data"])
        self.assertIn("不可用", result["items"][1]["error"])

    def test_failed_fetch_does_not_write_success_cache(self):
        with patch.object(fund_terms, "_cache_get", return_value=None), \
                patch.object(fund_terms, "_http_get", side_effect=OSError("断网")), \
                patch.object(fund_terms, "_cache_set") as save, self.assertRaises(OSError):
            fund_terms.fetch_fund_terms("050025")
        save.assert_not_called()


class EtfPremiumTests(unittest.TestCase):
    def test_sign_and_same_snapshot_price(self):
        result = etf_premium.parse_etf_snapshot(premium_payload(), "513500")
        self.assertEqual(result["premium_pct"], 10)
        self.assertIsNone(result["iopv_time"])
        self.assertEqual(result["price"], 1.1)

    def test_stale_future_and_missing_iopv_cannot_alert(self):
        result = etf_premium.parse_etf_snapshot(premium_payload(), "513500")
        stamp = datetime.fromisoformat(result["quote_time"])
        self.assertTrue(etf_premium.snapshot_status(result, stamp + timedelta(seconds=60))["alert_eligible"])
        for offset in [-1, 121, 86400]:
            self.assertFalse(etf_premium.snapshot_status(result, stamp + timedelta(seconds=offset))["alert_eligible"])
        missing = etf_premium.parse_etf_snapshot(premium_payload(f441="-", f402="-"), "513500")
        self.assertIsNone(missing["premium_pct"])
        self.assertFalse(etf_premium.snapshot_status(missing, stamp)["alert_eligible"])

    def test_invalid_numbers_identity_dates_and_inconsistent_rates_fail(self):
        for change in [{"f12": "513100"}, {"f297": 20260106}, {"f402": 10}, {"f441": 0},
                       {"f441": float("nan")}, {"f2": True}, {"f124": None}, {"f2": []}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                etf_premium.parse_etf_snapshot(premium_payload(**change), "513500")
        for payload in [None, [], {"data": []}, {"data": {"diff": {"0": {}}}}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                etf_premium.parse_etf_snapshot(payload, "513500")

    def test_cached_snapshot_rechecks_age(self):
        old = etf_premium.parse_etf_snapshot(premium_payload(), "513500")
        with patch.object(etf_premium, "_cache_get", return_value=old), patch.object(etf_premium, "_http_get") as fetch:
            result = etf_premium.fetch_etf_premium("513500")
        self.assertFalse(result["alert_eligible"])
        fetch.assert_not_called()

    def test_new_endpoints_reject_invalid_input_before_fetch(self):
        with patch.object(app, "collect_stages") as fetch:
            for code in ["", "../050025", "０５００２５", "1234567"]:
                self.assertTrue(app.handle_fund_terms({"symbol": [code]})["invalid"])
            for code in ["050025", "600000", "../513500", ""]:
                self.assertTrue(app.handle_etf_premium({"symbol": [code]})["invalid"])
            fetch.assert_not_called()

    def test_disclosed_nav_never_substitutes_for_missing_iopv(self):
        nav = {"history": [{"date": "2026-01-02", "nav": 1}], "source": "fixture",
               "source_url": "https://example.test/nav", "fetched_at": "2026-01-05T02:00:00Z"}
        with patch.object(app, "fetch_etf_premium", side_effect=OSError("IOPV缺失")), \
                patch.object(app, "fetch_fund_nav", return_value=nav):
            result = app.handle_etf_premium({"symbol": ["513500"]})
        self.assertIsNone(result["iopv"])
        self.assertEqual(result["disclosed_nav"]["date"], "2026-01-02")
        self.assertIsNone(result["disclosed_nav"]["premium_pct"])
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
