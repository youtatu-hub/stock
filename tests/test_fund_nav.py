from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app
from analysis.fund_nav import analyze_fund_nav
from data import fund_nav


def source_for(rows, symbol="050025"):
    return (f"var fS_code = {json.dumps(symbol)};"
            'var fS_name = "测试联接基金";'
            f"var Data_netWorthTrend = {json.dumps(rows)};")


def sample_history(count=100):
    start = datetime(2026, 1, 1)
    return [{"date": (start + timedelta(days=i)).date().isoformat(),
             "nav": 1.01 ** i, "change_pct": 1.0} for i in range(count)]


class FundSourceTests(unittest.TestCase):
    def test_dates_use_china_timezone_and_history_is_sorted(self):
        midnight = datetime(2026, 1, 1, 16, tzinfo=timezone.utc).timestamp() * 1000
        rows = [{"x": midnight + 86400000, "y": 1.1, "equityReturn": 10},
                {"x": midnight, "y": 1, "equityReturn": 0}]
        data = fund_nav.parse_fund_nav(source_for(rows), "050025")
        self.assertEqual(data["name"], "测试联接基金")
        self.assertEqual(data["history"][0]["date"], "2026-01-02")
        self.assertEqual(data["history"][-1]["date"], "2026-01-03")

    def test_rejects_wrong_fund_empty_history_and_invalid_numbers(self):
        row = {"x": 1767369600000, "y": 1, "equityReturn": 0}
        with self.assertRaisesRegex(ValueError, "代码"):
            fund_nav.parse_fund_nav(source_for([row], "015299"), "050025")
        with self.assertRaisesRegex(ValueError, "暂无"):
            fund_nav.parse_fund_nav(source_for([]), "050025")
        for field, value in [("y", 0), ("y", float("nan")), ("y", True), ("equityReturn", -100)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                fund_nav.parse_fund_nav(source_for([{**row, field: value}]), "050025")
        with self.assertRaisesRegex(ValueError, "重复日期"):
            fund_nav.parse_fund_nav(source_for([row, row]), "050025")

    def test_history_is_bounded_without_replacing_missing_growth_with_zero(self):
        rows = [{"x": 1704038400000 + i * 86400000, "y": 1, "equityReturn": None}
                for i in range(510)]
        result = fund_nav.parse_fund_nav(source_for(rows), "050025")
        self.assertEqual(len(result["history"]), fund_nav.NAV_HISTORY_COUNT)
        self.assertIsNone(result["history"][-1]["change_pct"])

    def test_cached_source_preserves_fetch_time_and_network_failure_is_visible(self):
        cached = {"symbol": "050025", "fetched_at": "2026-01-01T00:00:00Z"}
        with patch.object(fund_nav, "_cache_get", return_value=cached), patch.object(fund_nav, "_http_get") as get:
            self.assertIs(fund_nav.fetch_fund_nav("050025"), cached)
            get.assert_not_called()
        with patch.object(fund_nav, "_cache_get", return_value=None), \
                patch.object(fund_nav, "_http_get", side_effect=OSError("source unavailable")), \
                patch.object(fund_nav, "_cache_set") as save, self.assertRaisesRegex(OSError, "unavailable"):
            fund_nav.fetch_fund_nav("050025")
        save.assert_not_called()


class FundAnalysisTests(unittest.TestCase):
    def test_returns_use_calendar_periods_and_preserve_cached_input(self):
        points = sample_history()
        before = copy.deepcopy(points)
        result = analyze_fund_nav(points)
        self.assertEqual(points, before)
        self.assertEqual(result["analysis"]["direction"], "上升")
        for name, days in [("week", 7), ("month", 30), ("quarter", 90)]:
            self.assertAlmostEqual(result["analysis"]["returns"][name], round((1.01 ** days - 1) * 100, 2))
        self.assertEqual(result["analysis"]["max_drawdown_pct"], 0)
        self.assertEqual(result["history"][-1]["adjusted_nav"], points[-1]["nav"])

    def test_split_is_not_reported_as_a_crash(self):
        points = [{"date": "2026-01-01", "nav": 2, "change_pct": 0},
                  {"date": "2026-01-02", "nav": 1, "change_pct": 0}]
        result = analyze_fund_nav(points)
        self.assertEqual([p["adjusted_nav"] for p in result["history"]], [1, 1])
        self.assertEqual(result["history"][0]["nav"], 2)
        self.assertEqual(result["analysis"]["max_drawdown_pct"], 0)
        self.assertEqual(result["analysis"]["adjustment_days"], 1)

    def test_rounded_daily_growth_does_not_accumulate_return_error(self):
        points = [{"date": f"2026-01-{i + 1:02d}", "nav": round(1.013 ** i, 4), "change_pct": 1.3}
                  for i in range(8)]
        for i in range(1, len(points)):
            points[i]["change_pct"] = round((points[i]["nav"] / points[i - 1]["nav"] - 1) * 100, 2)
        result = analyze_fund_nav(points)
        self.assertEqual(result["analysis"]["returns"]["week"], round((points[-1]["nav"] / points[0]["nav"] - 1) * 100, 2))
        self.assertEqual(result["analysis"]["adjustment_days"], 0)

    def test_drawdown_and_insufficient_history(self):
        points = [{"date": f"2026-01-0{i + 1}", "nav": nav, "change_pct": growth}
                  for i, (nav, growth) in enumerate([(1, 0), (1.2, 20), (0.9, -25), (1, 100 / 9)])]
        result = analyze_fund_nav(points)
        self.assertEqual(result["analysis"]["max_drawdown_pct"], -25)
        self.assertEqual(result["analysis"]["direction"], "数据不足")
        self.assertIsNone(result["analysis"]["returns"]["week"])

    def test_missing_growth_stops_adjustment_and_does_not_produce_a_signal(self):
        points = sample_history()
        points[-1]["change_pct"] = None
        result = analyze_fund_nav(points)
        self.assertTrue(result["analysis"]["has_growth_gap"])
        self.assertEqual(result["analysis"]["sample_count"], 1)
        self.assertIsNone(result["analysis"]["max_drawdown_pct"])
        self.assertIsNone(result["history"][-2]["adjusted_nav"])
        self.assertNotIn("signal", result)


class FundHandlerTests(unittest.TestCase):
    def test_invalid_symbols_never_reach_upstream(self):
        with patch.object(app, "fetch_required") as fetch:
            for symbol in ["", "abc", "12345", "1234567", "../foo", "０５００２５"]:
                with self.subTest(symbol=symbol):
                    self.assertIn("error", app.handle_fund({"symbol": [symbol]}))
            fetch.assert_not_called()

    def test_fund_contract_keeps_nav_separate_from_stock_quotes(self):
        data = {"symbol": "050025", "name": "测试联接基金", "history": sample_history(),
                "source": "test", "fetched_at": "2026-01-01T00:00:00Z"}
        with patch.object(app, "fetch_fund_nav", return_value=data):
            result = app.handle_fund({"symbol": ["050025"]})
        self.assertEqual(result["latest"]["nav"], data["history"][-1]["nav"])
        self.assertIn("analysis", result)
        for field in ["open", "high", "low", "volume", "amount", "turnover"]:
            self.assertNotIn(field, result["latest"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
