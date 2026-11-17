from __future__ import annotations

import json
import math
import threading
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from analysis import chanlun_daily as chanlun
from analysis.breakout_module import analyze_breakout_system1
from analysis.signal_engine import _breakout_to_score, run_analysis
from data import kline_fetcher as fetcher
from data.market_breadth import MarketBreadthCache


def candidate():
    return {
        "action": "买入", "score": 70, "confidence": 70,
        "module_scores": dict.fromkeys(("趋势", "CAN_SLIM", "突破", "量价", "形态"), 70),
        "canslim": {"m_score": 60}, "trend": {"signals": []},
        "volume_price": {"signals": []}, "buy_signals": [], "sell_signals": [],
        "plain_summary": "出现买入信号，建议半仓入场，买入价1.26，目标1.28。",
        "trade_plan": {"action": "买入", "entry_price": 10, "stop_loss": 9,
                       "target_price": 12, "risk_reward_ratio": 2,
                       "position_size": "半仓(1/2)", "notes": "旧买入计划"},
    }


def sample_klines(count=100):
    start = date(2026, 7, 1)
    rows = []
    for i in range(count):
        close = 10 + math.sin(i * math.pi / 6)
        rows.append(fetcher.Kline(str(start + timedelta(days=i)), close, close,
                                  close + 0.2, close - 0.2, 1000,
                                  fetched_at="2026-11-16T08:00:00+00:00"))
    return rows


class SignalPolicyTests(unittest.TestCase):
    def test_hard_veto_replaces_summary_and_plan(self):
        data = candidate()
        data["volume_price"]["signals"] = ["OBV 下降"]
        data["trade_plan"].update(entry_price=1.26, stop_loss=1.20, target_price=1.28)
        result = app._apply_signal_optimization(data, [], None)
        self.assertEqual(result["action"], "观望")
        self.assertEqual(result["trade_plan"]["action"], "观望")
        self.assertEqual(result["position_advice"], "空仓等待")
        self.assertEqual(result["trade_plan"]["position_size"], "空仓等待")
        self.assertIn("OBV下降", result["plain_summary"])
        self.assertNotIn("出现买入信号", result["plain_summary"])
        self.assertNotIn("买入价", result["plain_summary"])
        for key in ("entry_price", "target_price", "stop_loss"):
            self.assertIsNone(result["trade_plan"][key])

    def test_low_zero_and_negative_reward_all_cancel_entry(self):
        for target in (10.99, 10, 9):
            with self.subTest(target=target):
                data = candidate()
                data["trade_plan"]["target_price"] = target
                result = app._apply_signal_optimization(data, [], None)
                self.assertEqual(result["action"], "观望")
                self.assertFalse(result["trade_plan"]["executable"])
                self.assertEqual(result["trade_plan"]["position_size"], "空仓等待")
                self.assertNotIn("旧买入计划", result["trade_plan"]["notes"])

    def test_invalid_stop_or_missing_prices_cancel_entry(self):
        for stop in (10, 11, None, float("nan")):
            with self.subTest(stop=stop):
                data = candidate()
                data["trade_plan"]["stop_loss"] = stop
                result = app._apply_signal_optimization(data, [], None)
                self.assertEqual(result["action"], "观望")
                json.dumps(result, allow_nan=False)

    def test_ratio_boundary_is_based_on_prices_not_cached_ratio(self):
        data = candidate()
        data["trade_plan"].update(entry_price=1.2, stop_loss=1.1, target_price=1.3,
                                  risk_reward_ratio=0)
        result = app._apply_signal_optimization(data, [], None)
        self.assertEqual(result["action"], "买入")
        self.assertEqual(result["trade_plan"]["risk_reward_ratio"], 1)

    def test_bearish_market_limits_strong_stock_without_forcing_watch(self):
        data = candidate()
        data.update(action="强烈买入", score=80)
        data["canslim"]["m_score"] = 15
        result = app._apply_signal_optimization(data, [], None)
        self.assertEqual(result["action"], "买入")
        self.assertTrue(result["trade_plan"]["executable"])
        self.assertIn("轻仓(1/4)", result["plain_summary"])
        self.assertNotIn("半仓", result["plain_summary"])
        self.assertEqual(result["action"], result["trade_plan"]["action"])

    def test_soft_veto_updates_all_action_fields(self):
        data = candidate()
        data["trend"]["signals"] = ["MA20向下"]
        result = app._apply_signal_optimization(data, [], None)
        self.assertEqual(result["action"], "谨慎买入")
        self.assertEqual(result["trade_plan"]["action"], "谨慎买入")
        self.assertIn("最终建议：谨慎买入", result["plain_summary"])


class BreakoutDirectionTests(unittest.TestCase):
    def test_short_stop_above_entry_is_explicit_and_not_bullish(self):
        rows = sample_klines(70)
        for i, row in enumerate(rows):
            row.open = row.close = 20 - i / 10
            row.high, row.low = row.close + 0.04, row.close - 0.04
        result = analyze_breakout_system1(rows)
        self.assertEqual(result.direction, "short")
        self.assertEqual(result.signal, "持仓空头")
        self.assertGreater(result.stop_loss, result.entry_price)
        self.assertEqual(result.breakout_price, result.entry_price)
        self.assertLess(_breakout_to_score([result]), 50)
        analysis = run_analysis(rows, index_klines=[])
        self.assertFalse(any("做空模型持仓" in text for text in analysis.buy_signals))
        self.assertTrue(any("做空模型持仓" in text for text in analysis.sell_signals))
        payload = app.signal_to_dict(analysis)
        self.assertEqual(payload["breakouts"][0]["direction"], "short")

    def test_long_stop_below_initial_entry_and_direction_survives_serialization(self):
        rows = sample_klines(70)
        for i, row in enumerate(rows):
            row.open = row.close = 10 + i / 10
            row.high, row.low = row.close + 0.04, row.close - 0.04
        result = analyze_breakout_system1(rows)
        self.assertEqual(result.direction, "long")
        self.assertLess(result.stop_loss, result.entry_price)
        self.assertGreater(_breakout_to_score([result]), 50)


class ChanlunConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.dates = [str(date(2026, 10, 1) + timedelta(days=i)) for i in range(21)]
        self.strokes = [
            chanlun.DailyStroke("up", 10, 12, self.dates[0], self.dates[4], 0, 1),
            chanlun.DailyStroke("down", 12, 10.5, self.dates[4], self.dates[8], 1, 2),
            chanlun.DailyStroke("up", 10.5, 13.5, self.dates[8], self.dates[12], 2, 3),
            chanlun.DailyStroke("down", 13.5, 12.5, self.dates[12], self.dates[16], 3, 4),
        ]

    def test_wick_outside_but_close_inside_is_not_a_breakout(self):
        zones = chanlun.find_zhongshus(self.strokes[:3], self.dates, [11] * 21)
        self.assertFalse(zones[0].is_broken)
        self.assertEqual(zones[0].current_position, "inside")
        self.assertFalse(zones[0].historical_break_direction)

    def test_historical_breakout_returning_inside_has_one_current_state(self):
        closes = [11] * 21
        closes[9:17] = [13] * 8
        zones = chanlun.find_zhongshus(self.strokes, self.dates, closes)
        self.assertEqual(zones[0].historical_break_direction, "up")
        self.assertEqual(zones[0].current_position, "inside")
        self.assertFalse(zones[0].is_broken)
        signals = chanlun.generate_daily_signals(self.strokes, [], zones)
        self.assertEqual(signals[0].type, "buy3")
        chanlun.mark_signal_validity(signals, self.dates, closes)
        self.assertTrue(signals[0].expired)
        self.assertIn("中枢边界", signals[0].expiry_reason)
        state, summary, _ = chanlun._describe_state(self.strokes, zones, signals, 4)
        self.assertIn("内", state)
        self.assertNotIn("空头延续", state)
        self.assertIn("暂无有效信号", summary)

    def test_close_exactly_on_boundary_remains_inside(self):
        for close in (10.5, 12):
            with self.subTest(close=close):
                zones = chanlun.find_zhongshus(self.strokes, self.dates, [close] * 21)
                self.assertEqual(zones[0].current_position, "inside")

    def test_third_buy_and_down_stroke_are_described_as_different_structures(self):
        closes = [11] * 9 + [13] * 12
        zones = chanlun.find_zhongshus(self.strokes, self.dates, closes)
        signals = chanlun.generate_daily_signals(self.strokes, [], zones)
        chanlun.mark_signal_validity(signals, self.dates, closes)
        state, summary, _ = chanlun._describe_state(self.strokes, zones, signals, 4)
        self.assertIn("最近完成笔向下", state)
        self.assertIn("向上突破", state)
        self.assertIn("方向与当前中枢位置不同", state)
        self.assertNotIn("空头延续", state)
        self.assertIn("三类买点", summary)

    def test_old_or_displaced_signals_expire_and_boundaries_are_inclusive(self):
        signals = [
            chanlun.ChanlunDailySignal("sell3", 1.19, self.dates[-1], ""),
            chanlun.ChanlunDailySignal("sell3", 1.67, self.dates[-1], ""),
            chanlun.ChanlunDailySignal("buy1", 2.07, self.dates[0], ""),
            chanlun.ChanlunDailySignal("buy1", 2.07, self.dates[-11], ""),
        ]
        chanlun.mark_signal_validity(signals, self.dates, [2.07] * 21)
        self.assertEqual([signal.expired for signal in signals], [True, True, True, False])
        exact_boundary = chanlun.ChanlunDailySignal("buy1", 10, self.dates[-1], "")
        chanlun.mark_signal_validity([exact_boundary], self.dates, [11] * 21)
        self.assertFalse(exact_boundary.expired)

    def test_analysis_uses_last_sixty_bars_and_selects_latest_valid_date(self):
        rows = sample_klines(100)
        signals = [
            chanlun.ChanlunDailySignal("sell1", rows[-1].close, rows[-1].date, "最新"),
            chanlun.ChanlunDailySignal("buy3", rows[-1].close, rows[-3].date, "旧信号"),
            chanlun.ChanlunDailySignal("buy2", rows[-1].close, rows[-30].date, "过期"),
        ]
        with patch.object(chanlun, "generate_daily_signals", return_value=signals):
            result = chanlun.analyze_chanlun_daily(*[[getattr(row, field) for row in rows]
                                                     for field in ("date", "open", "close", "high", "low", "volume")])
        payload = chanlun.daily_result_to_dict(result)
        self.assertEqual(payload["kline_count"], 60)
        self.assertEqual(payload["as_of"], rows[-1].date)
        self.assertEqual(payload["latest_signal"]["type"], "sell1")
        self.assertEqual(payload["signals"][0], payload["latest_signal"])
        self.assertIn("一类卖点", payload["summary"])
        self.assertEqual(len(payload["expired_signals"]), 1)
        self.assertEqual(len(payload["chart_signals"]), 2)
        self.assertTrue(all(zone.start_date >= rows[-60].date for zone in result.zhongshus))
        json.dumps(payload, allow_nan=False)


class DataProvenanceTests(unittest.TestCase):
    def test_quote_preserves_upstream_turnover_and_time_without_recalculation(self):
        source = {"data": {"f58": "测试ETF", "f43": 114.16, "f47": 10810000,
                           "f168": 137.6, "f86": 1794812400, "f124": 0}}
        with patch.object(fetcher, "_cache_get", return_value=None), \
                patch.object(fetcher, "_fetch_quote_tencent", return_value=None), \
                patch.object(fetcher, "_get_json_eastmoney", return_value=source) as request:
            quote = fetcher.fetch_quote("999711")
        self.assertEqual(quote.turnover, 137.6)
        self.assertEqual(quote.volume, 10810000)
        self.assertIn("f86", request.call_args.args[1]["fields"])
        payload = app.quote_to_dict(quote)
        self.assertTrue(payload["timestamp"])
        self.assertTrue(payload["fetched_at"])
        self.assertIn("东方财富", payload["turnover_source"])
        self.assertEqual(payload["volume_unit"], "手")

    def test_empty_flow_does_not_guess_market_session(self):
        with patch.object(app, "fetch_realtime_flow", return_value=[]):
            result = app.handle_realtime_flow({"symbol": ["513100"]})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("非交易日", json.dumps(result, ensure_ascii=False))
        self.assertTrue(result["checked_at"])

    def test_failed_flow_is_distinct_from_empty_successful_response(self):
        with patch.object(fetcher, "_cache_get", return_value=None), \
                patch.object(fetcher, "_fetch_realtime_flow_tencent", return_value=[]), \
                patch.object(fetcher, "_get_flow_json", return_value=None):
            with self.assertRaises(OSError):
                fetcher.fetch_realtime_flow("999712")
        with patch.object(fetcher, "_cache_get", return_value=None), \
                patch.object(fetcher, "_fetch_realtime_flow_tencent", return_value=[]), \
                patch.object(fetcher, "_get_flow_json", return_value={"data": {"klines": []}}):
            self.assertEqual(fetcher.fetch_realtime_flow("999712"), [])


class HttpSmokeTests(unittest.TestCase):
    def test_health_and_changed_endpoints_with_deterministic_data(self):
        rows = sample_klines()
        quote = fetcher.Quote("999713", "测试ETF", rows[-1].close, 1, 0.1, 11, 9, 10,
                              10, 1000, 10000, 137.6, timestamp="2026-11-16T07:00:00+00:00",
                              fetched_at="2026-11-16T08:00:00+00:00")
        with ExitStack() as stack:
            for name, value in (("fetch_kline", rows), ("fetch_quote", quote),
                                ("fetch_fund_flow", []), ("fetch_index_kline", []),
                                ("fetch_kline_extras", {}), ("fetch_realtime_flow", [])):
                stack.enter_context(patch.object(app, name, return_value=value))
            stack.enter_context(patch.object(app, "market_breadth_cache", MarketBreadthCache()))
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                results = {}
                for endpoint in ("health", "quote", "kline", "analyze", "chanlun_daily", "realtime_flow", "fund_flow"):
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("GET", f"/api/{endpoint}?symbol=999713")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, endpoint)
                    results[endpoint] = json.loads(response.read())
                    connection.close()
                self.assertEqual(results["health"]["status"], "ok")
                self.assertEqual(results["quote"]["turnover"], 137.6)
                self.assertTrue(results["kline"]["kline_status"]["fetched_at"])
                self.assertEqual(results["chanlun_daily"]["kline_count"], 60)
                signal = results["analyze"]["signal"]
                self.assertEqual(signal["action"], signal["trade_plan"]["action"])
                self.assertEqual(results["realtime_flow"]["status"], "unavailable")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
