from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import app
from data import etf_premium, kline_fetcher as fetcher
from data.ai_client import AIClient, normalize_base_url
from data.fund_flow_service import RealtimeFlowService, analysis_snapshot, history_status
from data.instruments import get_instrument
from data.market_breadth import MarketBreadthCache
from data.market_time import SHANGHAI
from test_asset_profiles import bars, premium_payload


class FlowAndQuoteRegressionTests(unittest.TestCase):
    def test_opening_hour_main_placeholder_remains_a_gap_without_component_backfill(self):
        start = datetime(2026, 11, 23, 9, 31)
        rows = [{"time": (start + timedelta(minutes=i)).strftime("%Y%m%d%H%M"),
                 "MainNetInflow": "0", "SuperNetInflow": "-6000", "BigNetInflow": "4000",
                 "NormalNetInflow": "1000", "SmallNetInflow": "1000",
                 "MainInflow": "10000", "MainOutflow": "12000"} for i in range(59)]
        rows.extend([{**rows[0], "time": "202611231030", "MainNetInflow": "-2000"},
                     {**rows[0], "time": "202611231500", "MainNetInflow": "-1000", "BigNetInflow": "5000"}])
        payload = {"code": 0, "data": {"todayFundTrend": {"stockCode": "sz159941", "minList": rows}}}
        with patch.object(fetcher, "_cache_get", return_value=None), patch.object(fetcher, "_cache_set"), \
                patch.object(fetcher, "flow_session_status", return_value={"session_state": "today"}), \
                patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: payload)), \
                patch.object(fetcher, "_fetch_realtime_flow_eastmoney", return_value=[]) as backup:
            result = fetcher.fetch_realtime_flow("159941")
        backup.assert_called_once_with("159941")
        self.assertEqual(result[0].time, "09:31")
        self.assertEqual(result[58].time, "10:29")
        self.assertTrue(all(row.main_net == 0 and not row.available for row in result[:59]))
        self.assertIn("未用分项补值", result[0].quality_note)
        response = RealtimeFlowService().snapshot("159941", lambda _: result)
        self.assertEqual((response["status"], response["minute_status"], response["missing_count"]), ("partial", "partial", 59))
        gap = response["missing_ranges"][0]
        self.assertEqual((gap["start"], gap["end"], gap["count"]), ("09:31", "10:29", 59))
        for row in response["flows"][:59]:
            for key in ("main_net", "super_large_net", "large_net", "medium_net", "small_net"):
                self.assertIsNone(row[key])
        self.assertEqual(response["flows"][59]["main_net"], -2000)
        self.assertTrue(response["summary_available"])
        self.assertEqual(response["summary"]["main_net"], -1000)
        self.assertEqual(response["summary"]["super_large_net"], -6000)
        original = rows[0]
        for change in ({"MainNetInflow": "100"}, {"BigNetInflow": "nan"}):
            rows[0] = {**original, **change}
            with patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: payload)), self.assertRaises(ValueError):
                fetcher._fetch_realtime_flow_tencent("159941")

    def test_valid_cumulative_plateau_and_larger_opening_outflow_remain_available(self):
        stamps = ["0931", "1456", "1457", "1458", "1459", "1500"]
        nets = [-20000, -9000, -9000, -9000, -9000, -8000]
        rows = [{"time": f"20261123{stamp}", "MainNetInflow": str(net),
                 "SuperNetInflow": "-25000", "BigNetInflow": str(net + 25000),
                 "NormalNetInflow": "0", "SmallNetInflow": str(-net)} for stamp, net in zip(stamps, nets)]
        payload = {"code": 0, "data": {"todayFundTrend": {"stockCode": "sh513300", "minList": rows}}}
        with patch.object(fetcher, "_http_get", return_value=Mock(json=lambda: payload)):
            result = fetcher._fetch_realtime_flow_tencent("513300")
        self.assertTrue(all(row.available for row in result))
        self.assertEqual([row.main_net for row in result], nets)

    def test_backup_opening_zero_placeholder_is_gap_and_bad_amount_is_not_zero(self):
        start = datetime(2026, 11, 23, 9, 31)
        lines = [f"{start + timedelta(minutes=i):%Y-%m-%d %H:%M},0,0,0,0,0" for i in range(60)]
        lines.append("2026-11-23 10:31,100,-50,-50,60,40")
        with patch.object(fetcher, "_get_flow_json", return_value={"data": {"klines": lines}}):
            rows = fetcher._fetch_realtime_flow_eastmoney("159941")
        response = RealtimeFlowService().snapshot("159941", lambda _: rows)
        self.assertEqual(response["status"], "partial")
        self.assertIsNone(response["flows"][0]["main_net"])
        self.assertEqual(response["flows"][-1]["main_net"], 100)
        self.assertIn("09:31", response["quality_note"])
        lines[0] = "2026-11-23 09:31,-,0,0,0,0"
        with patch.object(fetcher, "_get_flow_json", return_value={"data": {"klines": lines}}), self.assertRaises(ValueError):
            fetcher._fetch_realtime_flow_eastmoney("159941")

    def test_quote_detects_six_minute_lag_and_bypasses_shared_analysis_queue(self):
        quote = fetcher.Quote("159941", "测试", 1, 0, 0, 1, 1, 1, 1, 100, 10000, 1,
                              timestamp="2026-11-23T02:00:00+00:00")
        self.assertIn("360秒", fetcher.quote_delay_reason(quote, datetime(2026, 11, 23, 10, 6, tzinfo=SHANGHAI)))
        self.assertFalse(fetcher.quote_delay_reason(quote, datetime(2026, 11, 23, 12, 6, tzinfo=SHANGHAI)))
        with patch.object(app, "fetch_quote", return_value=quote), \
                patch.object(app, "collect_stages", side_effect=AssertionError("报价不应排入分析队列")):
            self.assertEqual(app.handle_quote({"symbol": ["159941"]})["price"], 1)
        fresh = replace(quote, timestamp="2026-11-23T02:06:00+00:00", source="东方财富")
        delay_reason = fetcher.quote_delay_reason
        now = datetime(2026, 11, 23, 10, 6, tzinfo=SHANGHAI)
        with patch.object(fetcher, "_cache_get", return_value=None), patch.object(fetcher, "_cache_set"), \
                patch.object(fetcher, "quote_delay_reason", side_effect=lambda value: delay_reason(value, now)), \
                patch.object(fetcher, "_fetch_quote_tencent", return_value=quote), \
                patch.object(fetcher, "_fetch_quote_eastmoney", return_value=fresh):
            self.assertEqual(fetcher.fetch_quote("159941"), fresh)


class ModelRegressionTests(unittest.TestCase):
    def test_core_and_all_a_shares_have_canslim_observation_assets_do_not(self):
        for code in ("512050", "588000", "159915", "600000", "000001", "300750"):
            self.assertTrue(get_instrument(code)["canslim"], code)
        for code in ("511360", "511090", "513500", "513300", "159501", "159941", "513100"):
            self.assertFalse(get_instrument(code)["canslim"], code)
        for code in ("512050", "588000", "159915", "511090"):
            with patch.object(app, "fetch_kline", return_value=bars()), \
                    patch.object(app, "fetch_quote", return_value=None), \
                    patch.object(app, "fetch_kline_extras", return_value={}), \
                    patch.object(app, "fetch_index_kline", return_value=bars()), \
                    patch.object(app, "fetch_fund_flow", return_value=[]):
                result = app.handle_analyze({"symbol": [code]})
            self.assertTrue(result["instrument"]["fund_flow"])
            self.assertTrue(result["signal"]["breakouts"])
            self.assertEqual(result["signal"]["canslim"] is not None, code != "511090")
            self.assertFalse(result["signal"]["trade_plan"]["executable"])

    def test_strong_branch_from_real_indicators_through_analyze_handler(self):
        start = date(2026, 1, 1)
        rows = [fetcher.Kline(str(start + timedelta(days=i)), 10+i*.02, 10+i*.02,
                             10.02+i*.02, 9.98+i*.02, 1000) for i in range(250)]
        for row, close in zip(rows[-8:], [14.81, 14.78, 14.75, 14.72, 14.70, 14.68, 14.66, 15.50]):
            row.open = row.close = close
            row.high, row.low, row.volume = close + .02, close - .02, 3000
        rows[-1].volume = 3600
        index = [replace(row, close=100-i, high=101-i, low=99-i) for i, row in enumerate(rows[-60:])]
        with patch.object(app, "fetch_kline", return_value=rows), patch.object(app, "fetch_quote", return_value=None), \
                patch.object(app, "fetch_kline_extras", return_value={}), patch.object(app, "fetch_fund_flow", return_value=[]), \
                patch.object(app, "fetch_index_kline", return_value=index), patch.object(app, "market_breadth_cache", MarketBreadthCache()):
            signal = app.handle_analyze({"symbol": ["600000"]})["signal"]
        self.assertEqual(signal["canslim"]["m_score"], 15)
        self.assertIn("OBV下降", signal["volume_price"]["signals"])
        self.assertTrue(signal["strong_new_high"])
        self.assertEqual(signal["action"], "谨慎买入")
        self.assertTrue(signal["trade_plan"]["executable"])
        self.assertIn("轻仓", signal["position_advice"])


class PremiumRegressionTests(unittest.TestCase):
    def test_tencent_keeps_nav_separate_and_requires_independent_time_for_alert(self):
        fields = [""] * 83
        for index, value in {1: "测试ETF", 2: "513500", 3: "1.2", 30: "20260105100000",
                             77: "20", 78: "1", 81: "1.055", 82: "CNY"}.items():
            fields[index] = value
        snapshot = etf_premium.parse_tencent_snapshot('v_sh513500="' + "~".join(fields) + '";', "513500")
        self.assertEqual(snapshot["iopv"], 1)
        self.assertEqual(snapshot["premium_pct"], 20)
        current = etf_premium.snapshot_status(snapshot, datetime.fromisoformat(snapshot["quote_time"]))
        self.assertEqual(current["status"], "unavailable")
        self.assertIsNone(current["iopv"])
        self.assertIsNone(current["premium_pct"])
        self.assertEqual(current["reported_reference"]["value"], 1)
        self.assertEqual(current["reported_reference"]["premium_pct"], 20)
        self.assertEqual(current["reported_reference"]["source_price"], 1.2)
        self.assertEqual(current["reported_reference"]["price_time"], snapshot["quote_time"])
        self.assertIsNone(current["reported_reference"]["valuation_time"])
        self.assertFalse(current["alert_eligible"])
        self.assertEqual(snapshot["source"], "腾讯")

    def test_upstream_failures_keep_reference_time_and_nav_uses_separate_quote(self):
        snapshot = etf_premium.parse_etf_snapshot(premium_payload(), "513500")
        snapshot["fetched_at"] = "2026-01-05T02:00:01Z"
        with patch.object(etf_premium, "_cache_get", side_effect=lambda key, ttl: snapshot if ttl == 300 else None), \
                patch.object(etf_premium, "_cache_set"), patch.object(etf_premium, "_http_get", side_effect=OSError("HTTP 502")):
            stale = etf_premium.fetch_etf_premium("513500")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["fetched_at"], snapshot["fetched_at"])
        self.assertEqual(stale["quote_time"], snapshot["quote_time"])
        self.assertFalse(stale["alert_eligible"])
        self.assertIsNone(stale["iopv"])
        self.assertIsNone(stale["premium_pct"])
        self.assertFalse(stale["reported_reference"]["usable_for_analysis"])
        self.assertEqual(stale["reported_reference"]["premium_pct"], 10)
        self.assertEqual(stale["reported_reference"]["fetched_at"], snapshot["fetched_at"])
        quote = fetcher.Quote("513500", "测试", 1.2, 0, 0, 1, 1, 1, 1, 1, 1, 1)
        nav = {"history": [{"date": "2026-01-02", "nav": 1}], "source": "fixture", "source_url": "", "fetched_at": ""}
        with patch.object(app, "fetch_etf_premium", side_effect=OSError("IOPV缺失")), \
                patch.object(app, "fetch_quote", return_value=quote), patch.object(app, "fetch_fund_nav", return_value=nav):
            result = app.handle_etf_premium({"symbol": ["513500"]})
        self.assertEqual(result["disclosed_nav"]["premium_pct"], 20)
        self.assertIsNone(result["iopv"])


class AIFlowContextTests(unittest.TestCase):
    def test_five_overseas_etfs_use_page_snapshot_and_separate_three_flow_states(self):
        service = RealtimeFlowService()
        rows = [fetcher.MinuteFlow("09:31", 0, 1000, 1000, 4000, -6000, date="2026-11-23",
                                  source="腾讯", available=False, quality_note="原始主力缺失"),
                fetcher.MinuteFlow("15:00", -2000, 1000, 1000, 4000, -6000, date="2026-11-23", source="腾讯")]
        premium = etf_premium.snapshot_status(etf_premium.parse_etf_snapshot(premium_payload(), "513500"))
        for symbol in ("513500", "513300", "159501", "159941", "513100"):
            with self.subTest(symbol=symbol):
                page = service.snapshot(symbol, lambda _: rows)
                # 页面提交快照编号后服务已刷新，模型仍须读取页面那一版。
                service.snapshot(symbol, lambda _: [replace(rows[-1], main_net=5000, large_net=11000)], force=True)
                with patch.object(app, "realtime_flow_service", service), \
                        patch.object(app, "AIClient") as client_factory, \
                        patch.object(app, "handle_analyze", return_value={"symbol": symbol, "flow_status": history_status([], requested=False)}), \
                        patch.object(app, "handle_etf_premium", return_value={"iopv": premium}), \
                        patch.object(app, "handle_realtime_flow", side_effect=AssertionError("不得另取一版实时流")):
                    client = client_factory.return_value
                    client.model = "fixture-model"
                    client.analyze.return_value = "fixture"
                    result = app.handle_ai_analysis({"symbol": symbol, "flow_snapshot": {"id": page["snapshot_id"], "status": page["status"]}})
                context = client.analyze.call_args.args[0]
                realtime = context["fund_flow"]["realtime"]
                self.assertEqual(realtime, analysis_snapshot(page))
                self.assertTrue(realtime["summary_available"])
                self.assertEqual(realtime["summary"]["main_net"], -2000)
                self.assertEqual((realtime["minute_status"], realtime["missing_count"]), ("partial", 1))
                daily = context["fund_flow"]["daily_history"]
                self.assertEqual(daily["status"], "not_requested")
                self.assertIsNone(daily["available"])
                self.assertNotIn("flow_status", context)
                self.assertEqual(result["fund_flow"], context["fund_flow"])
                self.assertIsNone(context["premium_reference"]["iopv"]["iopv"])
                self.assertNotIn("reported_reference", context["premium_reference"]["iopv"])
                self.assertNotIn("reported_premium_pct", context["premium_reference"]["iopv"])
                self.assertIsNone(context["premium_reference"]["iopv"]["premium_pct"])

    def test_api_without_page_reference_uses_shared_realtime_handler(self):
        page = RealtimeFlowService().snapshot("600000", lambda _: [])
        with patch.object(app, "AIClient") as client_factory, \
                patch.object(app, "handle_analyze", return_value={"symbol": "600000", "flow_status": history_status([])}), \
                patch.object(app, "handle_realtime_flow", return_value=page) as realtime:
            client_factory.return_value.model = "fixture-model"
            client_factory.return_value.analyze.return_value = "fixture"
            result = app.handle_ai_analysis({"symbol": "600000"})
        realtime.assert_called_once_with({"symbol": ["600000"]})
        self.assertEqual(result["fund_flow"]["realtime"], analysis_snapshot(page))
        self.assertEqual(result["fund_flow"]["daily_history"]["status"], "unavailable")


class CPAIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls

        class CPA(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization"), None))
                self.respond({"data": [{"id": "fixture-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("Authorization"), body))
                self.respond({"choices": [{"message": {"content": "强势分支：结合本次数据评估量价与风险。"}}]})

            def respond(self, body):
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CPA)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"base_url": f"http://127.0.0.1:{self.server.server_port}", "api_key": "fixture-key", "model": "fixture-model"}
        self.enterContext(patch("data.ai_client.load_cpa_config", return_value=self.config))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_models_and_analysis_through_actual_http_with_bearer_and_selected_model(self):
        client = AIClient("fixture-model")
        listing = client.models()
        self.assertEqual(listing["models"], ["fixture-model"])
        self.assertNotIn("base_url", listing)
        self.assertNotIn("api_key", listing)
        with patch.object(app, "handle_analyze", return_value={"symbol": "600000", "signal": {"action": "谨慎买入"}}):
            result = app.handle_ai_analysis({"model": "fixture-model", "symbol": "600000",
                                            "flow_snapshot": {"id": None, "status": "error"}})
        self.assertIn("强势", result["text"])
        self.assertEqual([call[0] for call in self.calls], ["/v1/models", "/v1/chat/completions"])
        self.assertTrue(all(call[1] == "Bearer fixture-key" for call in self.calls))
        body = self.calls[-1][2]
        self.assertEqual(body["model"], "fixture-model")
        self.assertIn("谨慎买入", body["messages"][-1]["content"])
        self.assertNotIn("fixture-key", json.dumps(result) + body["messages"][-1]["content"])

    def test_url_validation_and_client_only_accepts_model_selection(self):
        for value in ("file:///tmp/key", "https://user:password@example.test", "https://example.test?key=secret", "https://exa\nmple.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)
        with self.assertRaises(ValueError):
            AIClient({"base_url": "http://elsewhere.test/v1"})
        self.assertEqual(normalize_base_url("https://example.test/v1/models"), "https://example.test/v1")

    def test_api_post_validation_and_cross_origin_rejection(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            for body, origin, status in (({}, None, 200), ([], None, 400),
                                         ({"base_url": "http://elsewhere.test", "api_key": "other-key"}, None, 400),
                                         ({}, "https://unrelated.test", 403)):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                connection.request("POST", "/api/ai/models", json.dumps(body), headers)
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                text = response.read().decode()
                self.assertNotIn("fixture-key", text)
                self.assertNotIn(self.config["base_url"], text)
            connection.request("GET", "/api/ai/config")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
