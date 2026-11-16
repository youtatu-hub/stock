from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from analysis.volume_price_module import _analyze_fund_flow
from data import kline_fetcher as fetcher
from data.fund_flow_service import RealtimeFlowService, history_status
from data.request_runtime import collect_stages


class FundFlowTests(unittest.TestCase):
    def minute(self):
        return fetcher.MinuteFlow("15:00", 10000, -5000, -5000, 4000, 6000,
                                  date="2026-11-16", fetched_at="2026-11-16T07:00:01+00:00")

    def test_historical_sources_choose_newest_date(self):
        def response(url, **kwargs):
            date = "2000-01-03" if "new.example" in url else "2000-01-01"
            return Mock(json=Mock(return_value={"data": {"klines": [f"{date},1,2,3,4,5,6"]}}))
        with patch.object(fetcher, "_http_get", side_effect=response):
            data = fetcher._get_flow_json("/flow", {}, ["https://old.example", "https://new.example"])
        self.assertTrue(data["data"]["klines"][0].startswith("2000-01-03"))
        self.assertTrue(data["fetched_at"])

    def test_slow_backup_does_not_discard_available_history_at_parent_deadline(self):
        release = threading.Event()

        def response(url, **kwargs):
            if "slow.example" in url:
                release.wait(1)
                raise OSError("slow backup")
            return Mock(json=Mock(return_value={"data": {"klines": ["2000-01-03,1,2,3,4,5,6"]}}))

        try:
            with patch.object(fetcher, "_http_get", side_effect=response):
                values, errors, _ = collect_stages({
                    "flow": lambda: fetcher._get_flow_json(
                        "/flow", {}, ["https://available.example", "https://slow.example"]),
                }, required=("flow",), budget=0.3)
                self.assertFalse(errors)
                self.assertTrue(values["flow"]["data"]["klines"])
        finally:
            release.set()

    def test_realtime_parser_retains_trading_date_and_fetch_time(self):
        payload = {"fetched_at": "2026-11-16T07:00:01+00:00", "data": {
            "klines": ["2026-11-13 15:00,20000,-10000,-10000,8000,12000",
                       "2026-11-16 15:00,10000,-5000,-5000,4000,6000"]}}
        with patch.object(fetcher, "_cache_get", return_value=None), \
                patch.object(fetcher, "_cache_set"), \
                patch.object(fetcher, "_get_flow_json", return_value=payload):
            rows = fetcher.fetch_realtime_flow("999991")
        self.assertEqual(rows[0].date, "2026-11-16")
        self.assertEqual(rows[0].fetched_at, payload["fetched_at"])
        self.assertEqual(rows[0].main_net, 10000)
        self.assertEqual(len(rows), 1)

    def test_slow_source_is_bounded_and_failure_has_retry_cooldown(self):
        release = threading.Event()
        service = RealtimeFlowService(budget=0.03)
        source = Mock(side_effect=lambda symbol: release.wait(1) and [self.minute()])
        try:
            started = time.monotonic()
            result = service.snapshot("999992", source)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["retry_after_seconds"], 30)
            self.assertTrue(result["checked_at"])
            self.assertIsNone(result["fetched_at"])
            again = service.snapshot("999992", source)
            self.assertEqual(again["status"], "error")
            source.assert_called_once()
        finally:
            release.set()

    def test_retry_recovers_and_old_snapshot_keeps_its_timestamp(self):
        service = RealtimeFlowService()
        source = Mock(side_effect=[[self.minute()], OSError("HTTP 403"), OSError("HTTP 403"), [self.minute()]])
        good = service.snapshot("999993", source)
        failed = service.snapshot("999993", source, force=True)
        self.assertEqual(failed["status"], "stale")
        self.assertEqual(failed["fetched_at"], good["fetched_at"])
        self.assertEqual(failed["as_of"], "2026-11-16 15:00")
        self.assertIn("403", failed["message"])
        with patch("data.fund_flow_service.FLOW_SNAPSHOT_SECONDS", -1):
            expired = service.snapshot("999993", source, force=True)
        self.assertEqual(expired["status"], "error")
        self.assertFalse(expired["flows"])
        self.assertEqual(service.snapshot("999993", source, force=True)["status"], "ok")

    def test_latest_single_day_and_five_day_sum_have_distinct_dates(self):
        dates = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
        nets = [30000000, 30000000, 10000000, 10000000, -55000000]
        flows = [fetcher.FundFlow(date, net, 0, 0, 0, 0) for date, net in zip(dates, nets)]
        text, _ = _analyze_fund_flow(flows)
        self.assertIn("2026-11-12单日主力大幅流出", text)
        self.assertNotIn("今日", text)
        self.assertGreater(sum(row.main_net for row in flows), 0)
        klines = [fetcher.Kline(date, 1, 1, 1, 1, 1) for date in
                  ("2026-11-12", "2026-11-13", "2026-11-16")]
        status = history_status(flows, klines)
        self.assertTrue(status["lagging"])
        self.assertEqual(status["lag_trading_days"], 2)

    def test_breadth_preserves_source_error_and_uses_production_endpoint(self):
        with patch.object(fetcher, "_get_json_eastmoney", side_effect=OSError("push2.eastmoney.com：HTTP 403")) as source:
            with self.assertRaisesRegex(OSError, "第1页.*403"):
                fetcher._fetch_breadth_page(1)
        self.assertIn("ut", source.call_args.args[1])
        self.assertEqual(source.call_args.args[2][0], "https://push2.eastmoney.com")


if __name__ == "__main__":
    unittest.main()
