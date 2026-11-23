from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from analysis.volume_price_module import _analyze_fund_flow
from data import kline_fetcher as fetcher
from data.fund_flow_service import RealtimeFlowService, analysis_snapshot, history_status
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
                patch.object(fetcher, "_fetch_realtime_flow_tencent", return_value=[]), \
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

    def test_stale_snapshot_preserves_actual_source_and_amounts(self):
        service = RealtimeFlowService()
        source = Mock(side_effect=[[replace(self.minute(), source="腾讯")], OSError("数据源超时")])
        good = service.snapshot("999994", source)
        stale = service.snapshot("999994", source, force=True)
        self.assertEqual(good["source"], "腾讯")
        self.assertIn("腾讯口径", good["source_note"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["source"], "腾讯")
        self.assertEqual(stale["summary"], good["summary"])
        self.assertEqual(stale["fetched_at"], good["fetched_at"])
        self.assertTrue(stale["summary_available"])
        self.assertIn("旧快照", stale["availability_note"])

    def test_analysis_reference_keeps_page_version_after_refresh_and_preserves_stale_time(self):
        service = RealtimeFlowService()
        first = service.snapshot("513300", lambda _: [self.minute()])
        second = service.snapshot("513300", lambda _: [replace(self.minute(), main_net=20000)], force=True)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        reference = {"id": first["snapshot_id"], "status": first["status"]}
        self.assertEqual(service.for_analysis("513300", reference), analysis_snapshot(first))
        stale = service.for_analysis("513300", {**reference, "status": "stale"})
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["summary"], first["summary"])
        self.assertEqual(stale["as_of"], first["as_of"])
        self.assertIn("旧快照", stale["availability_note"])
        with self.assertRaisesRegex(ValueError, "品种"):
            service.for_analysis("159941", reference)
        with self.assertRaisesRegex(ValueError, "状态"):
            service.for_analysis("513300", {**reference, "status": "unavailable"})
        with patch("data.fund_flow_service.FLOW_SNAPSHOT_SECONDS", -1), self.assertRaisesRegex(ValueError, "过期"):
            service.for_analysis("513300", reference)

    def test_page_without_snapshot_and_last_minute_gap_do_not_claim_available_cumulative(self):
        service = RealtimeFlowService()
        missing = service.for_analysis("513300", {"id": None, "status": "error"})
        self.assertFalse(missing["summary_available"])
        self.assertEqual(missing["minute_status"], "unavailable")
        self.assertFalse(missing["summary"])
        data = service.snapshot("513300", lambda _: [
            replace(self.minute(), time="14:59"), replace(self.minute(), available=False, quality_note="不可核验")])
        self.assertFalse(data["summary_available"])
        self.assertTrue(all(value is None for value in data["summary"].values()))
        self.assertEqual(data["missing_ranges"][0]["start"], "15:00")

    def test_daily_history_not_requested_is_distinct_from_failed_fetch(self):
        not_requested = history_status([], requested=False)
        unavailable = history_status([])
        self.assertEqual(not_requested["status"], "not_requested")
        self.assertIsNone(not_requested["available"])
        self.assertFalse(not_requested["requested"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertFalse(unavailable["available"])
        self.assertTrue(unavailable["requested"])

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


class TencentFlowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(fetcher, "_cache_get", return_value=None))
        self.enterContext(patch.object(fetcher, "_cache_set"))
        self.enterContext(patch.object(fetcher, "flow_session_status", return_value={"session_state": "preopen"}))

    @staticmethod
    def payload():
        def row(stamp, main, large, super_large):
            return {"time": stamp, "MainNetInflow": str(main),
                    "BigNetInflow": str(large), "SuperNetInflow": str(super_large),
                    "NormalNetInflow": "-2000", "SmallNetInflow": "-8000"}
        return {"code": 0, "data": {"todayFundTrend": {"stockCode": "sh513300", "minList": [
            row("202611161500", 10000, 4000, 6000),
            row("202611131500", 30000, 10000, 20000),
            row("202611160931", 5000, 2000, 3000),
        ]}}}

    def test_tencent_success_never_calls_eastmoney_or_sums_cumulative_values(self):
        with patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: self.payload())) as http, \
                patch.object(fetcher, "_get_flow_json") as backup:
            rows = fetcher.fetch_realtime_flow("513300")
        backup.assert_not_called()
        http.assert_called_once_with(fetcher.TENCENT_FLOW,
                                     params={"code": "sh513300", "type": "todayFundTrend"})
        self.assertEqual([row.time for row in rows], ["09:31", "15:00"])
        self.assertEqual(rows[-1].main_net, 10000)
        self.assertEqual(rows[-1].super_large_net, 6000)
        self.assertEqual(rows[-1].large_net, 4000)
        self.assertEqual(rows[-1].medium_net, -2000)
        self.assertEqual(rows[-1].small_net, -8000)
        self.assertEqual(rows[-1].source, "腾讯")
        self.assertEqual(rows[-1].date, "2026-11-16")
        self.assertTrue(rows[-1].fetched_at)

    def test_tencent_rejects_bad_amounts_dates_and_wrong_symbol(self):
        for field, value in (("MainNetInflow", None), ("SuperNetInflow", "nan"),
                             ("BigNetInflow", "inf"), ("time", "202602301500")):
            with self.subTest(field=field, value=value):
                payload = self.payload()
                payload["data"]["todayFundTrend"]["minList"][0][field] = value
                with patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: payload)):
                    with self.assertRaises(ValueError):
                        fetcher._fetch_realtime_flow_tencent("513300")
        with patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: self.payload())):
            with self.assertRaisesRegex(ValueError, "品种"):
                fetcher._fetch_realtime_flow_tencent("159941")

    def test_tencent_empty_or_invalid_response_uses_complete_eastmoney_series(self):
        backup_payload = {"data": {"klines": ["2026-11-16 15:00,-10000,6000,4000,-3000,-7000"]}}
        for payload in ({"code": 0, "data": {"todayFundTrend": None}},
                        {"code": 1, "msg": "no data"}, self.payload()):
            # 此次请求159941；第三个响应的513300记录必须被拒绝。
            with self.subTest(payload=payload), \
                    patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: payload)), \
                    patch.object(fetcher, "_get_flow_json", return_value=backup_payload):
                rows = fetcher.fetch_realtime_flow("159941")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].source, "东方财富")
                self.assertEqual(rows[0].main_net, -10000)
                self.assertEqual(rows[0].super_large_net, -7000)

    def test_both_failures_name_sources_and_do_not_return_fake_empty_success(self):
        with patch.object(fetcher, "_http_get", side_effect=fetcher.requests.ConnectionError("offline")), \
                patch.object(fetcher, "_get_flow_json", side_effect=OSError("push2：HTTP 502")):
            with self.assertRaisesRegex(OSError, "腾讯.*东方财富.*502"):
                fetcher.fetch_realtime_flow("513300")

    def test_partial_sources_are_selected_as_whole_series_without_regressing_last_minute(self):
        gap = fetcher.MinuteFlow("09:31", 0, 1000, 1000, 4000, -6000, date="2026-11-16",
                                source="腾讯", available=False, quality_note="主力占位")
        final = replace(gap, time="15:00", main_net=-2000, available=True, quality_note="")
        primary = [gap, final]
        for backup, expected in (([replace(final, source="东方财富", time="14:59")], primary),
                                 ([replace(final, source="东方财富", main_net=-3000)], None)):
            with patch.object(fetcher, "_fetch_realtime_flow_tencent", return_value=primary), \
                    patch.object(fetcher, "_fetch_realtime_flow_eastmoney", return_value=backup):
                result = fetcher.fetch_realtime_flow("513300")
            self.assertEqual(result, expected if expected is not None else backup)
            self.assertEqual(len({row.source for row in result}), 1)

    def test_slow_primary_http_leaves_time_for_backup(self):
        release = threading.Event()
        paths = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                paths.append(self.path)
                if self.path.startswith("/tencent"):
                    release.wait(2)
                    return
                body = json.dumps({"data": {"klines": [
                    "2026-11-16 15:00,10000,-5000,-5000,4000,6000"]}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(fetcher, "TENCENT_FLOW", url + "/tencent"), \
                    patch.object(fetcher, "RT_FLOW_HOSTS", [url]), \
                    patch.object(fetcher, "FLOW_PRIMARY_BUDGET_SECONDS", 0.1):
                started = time.monotonic()
                rows = fetcher.fetch_realtime_flow("513300")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(rows[-1].source, "东方财富")
                self.assertEqual(rows[-1].main_net, 10000)
            self.assertEqual(len(paths), 2)
            self.assertTrue(paths[1].startswith("/api/qt/stock/fflow/kline/get"))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
