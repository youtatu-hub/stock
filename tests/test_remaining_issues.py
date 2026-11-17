from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from analysis import chanlun_daily as chanlun
from analysis.signal_engine import _build_trade_plan, run_analysis
from data import kline_fetcher as fetcher
from data.market_time import SHANGHAI, flow_session_status
from data.market_breadth import MarketBreadthCache
from test_signal_consistency import candidate, sample_klines


class RemainingIssueTests(unittest.TestCase):
    def test_remote_zone_is_historical_even_when_signal_is_recent(self):
        dates = [row.date for row in sample_klines(60)]
        strokes = [
            chanlun.DailyStroke("up", 1.199, 1.213, dates[-12], dates[-10], 0, 1),
            chanlun.DailyStroke("down", 1.213, 1.199, dates[-10], dates[-8], 1, 2),
            chanlun.DailyStroke("up", 1.199, 1.22, dates[-8], dates[-6], 2, 3),
        ]
        zones = chanlun.find_zhongshus(strokes, dates, [1.019] * 60)
        self.assertTrue(zones[0].expired)
        self.assertIn("偏离现价", zones[0].expiry_reason)
        signal = chanlun.ChanlunDailySignal("sell3", 1.062, dates[-1], "")
        chanlun.mark_signal_validity([signal], dates, [1.019] * 60)
        self.assertFalse(signal.expired)
        state, _, _ = chanlun._describe_state(strokes, zones, [signal], 0)
        self.assertIn("暂无有效中枢", state)
        self.assertFalse(chanlun._build_chart_overlay([], strokes, zones, [signal], dates)[2])
        old = [replace(s, start_date=dates[i * 2], end_date=dates[i * 2 + 2]) for i, s in enumerate(strokes)]
        zone = chanlun.find_zhongshus(old, dates, [1.205] * 60)[0]
        self.assertTrue(zone.expired)
        self.assertIn("形成后已过", zone.expiry_reason)

    def test_preopen_and_delayed_today_are_distinct_and_use_shanghai_time(self):
        preopen = flow_session_status("2026-11-16", datetime(2026, 11, 17, 9, 20, tzinfo=SHANGHAI))
        self.assertEqual(preopen["session_state"], "preopen")
        self.assertIn("2026-11-16", preopen["session_note"])
        late = flow_session_status("2026-11-16", datetime(2026, 11, 17, 10, tzinfo=SHANGHAI))
        self.assertEqual(late["session_state"], "awaiting_today")
        self.assertFalse(late["is_today"])
        today = flow_session_status("2026-11-17", datetime(2026, 11, 17, 10, tzinfo=SHANGHAI))
        self.assertTrue(today["is_today"])

    def test_old_primary_flow_does_not_mask_current_backup(self):
        old = fetcher.MinuteFlow("15:00", 6e6, 0, 0, 689e6, -683e6,
                                 date="2026-11-16", source="腾讯")
        new = replace(old, time="09:31", date="2026-11-17", source="东方财富")
        with patch.object(fetcher, "_cache_get", return_value=None), \
                patch.object(fetcher, "_cache_set"), \
                patch.object(fetcher, "flow_session_status", side_effect=lambda day: {"session_state": "today" if day == new.date else "awaiting_today"}), \
                patch.object(fetcher, "_fetch_realtime_flow_tencent", return_value=[old]), \
                patch.object(fetcher, "_fetch_realtime_flow_eastmoney", return_value=[new]):
            result = fetcher.fetch_realtime_flow("999781")
        self.assertEqual(result, [new])
        self.assertEqual(old.main_net, old.super_large_net + old.large_net)

    def test_turnover_denominator_and_quote_amount_checks(self):
        quote = fetcher.Quote("999782", "测试", 114.167, 0, 0, 114.174, 114.166,
                              114.167, 114.166, 10809655, 123412733473, 135.38,
                              float_shares=798478608)
        check = fetcher.quote_turnover_check(quote)
        self.assertEqual(check["status"], "consistent")
        self.assertEqual(check["calculated_pct"], 135.38)
        fetcher._validate_quote(quote)
        with self.assertRaisesRegex(ValueError, "成交量与成交额不一致"):
            fetcher._validate_quote(replace(quote, volume=quote.volume * 10))
        self.assertEqual(fetcher.quote_turnover_check(replace(quote, float_shares=1e9))["status"], "mismatch")
        with patch.object(fetcher, "_get_json_eastmoney", return_value={"data": {"f57": "999783"}}):
            with self.assertRaisesRegex(ValueError, "品种"):
                fetcher._fetch_quote_eastmoney("999782")

    def test_same_klines_score_identically_with_missing_or_preopen_quote(self):
        rows = sample_klines(250)
        quote = fetcher.Quote("999784", "测试", rows[-1].close, 0, 0, 0, 0, 0, rows[-1].close, 0, 0, 0)
        with patch.object(fetcher, "fetch_index_kline", side_effect=AssertionError("分析不应联网")):
            without = run_analysis(rows, index_klines=None)
            preopen = run_analysis(rows, quote=quote, index_klines=None)
        self.assertEqual(without.score, preopen.score)
        self.assertEqual(without.module_scores, preopen.module_scores)
        self.assertEqual(without.volume_price.volume_ratio, preopen.volume_price.volume_ratio)
        self.assertEqual(without.canslim.m_score, 50)
        first = app._analysis_inputs(rows, [], [], None, {})
        self.assertEqual(first["id"], app._analysis_inputs([replace(k, fetched_at="new") for k in rows], [], [], None, {})["id"])
        changed = rows[:-1] + [replace(rows[-1], close=rows[-1].close + 0.1)]
        self.assertNotEqual(first["id"], app._analysis_inputs(changed, [], [], None, {})["id"])

    def test_preopen_placeholder_bar_cannot_change_yesterday_analysis(self):
        rows = sample_klines(60)
        today = replace(rows[-1], date="2026-11-17", volume=0)
        with patch.object(fetcher, "flow_session_status", return_value={
                "session_state": "preopen", "market_date": "2026-11-17"}):
            self.assertEqual(fetcher.exclude_preopen_bar(rows + [today]), rows)

    def test_strong_new_high_has_own_obv_branch_but_keeps_price_veto(self):
        rows = sample_klines(250)
        rows[-1] = replace(rows[-1], close=12, high=12.1)
        for bad_price in (False, True):
            data = candidate()
            data["trend"].update(strength=100, signals=["跌破MA20"] if bad_price else [])
            data["volume_price"].update(signals=["OBV下降"], volume_ratio=1.2)
            data["canslim"].update(n_score=100, m_score=15)
            result = app._apply_signal_optimization(data, rows, None)
            self.assertTrue(result["strong_new_high"])
            self.assertEqual(result["action"], "观望" if bad_price else "谨慎买入")
            self.assertEqual(result["trade_plan"]["executable"], not bad_price)
            if not bad_price:
                self.assertIn("轻仓", result["position_advice"])

    def test_broken_resistance_cannot_be_selected_as_profit_target(self):
        rows = sample_klines(100)
        rows[-1] = replace(rows[-1], close=1.263)
        box = SimpleNamespace(direction="看涨", target_price=1.26, name="箱体", key_levels={"箱体上沿": 1.26})
        cs = SimpleNamespace(cup_handle={"breakout": True, "buy_point": 1.26, "target": 1.39})
        plan = _build_trade_plan("买入", 72, "中", "中", None, [box], [], cs, rows)
        self.assertEqual(plan["target_price"], 1.39)
        self.assertGreaterEqual(plan["risk_reward_ratio"], 2)

    def test_breadth_updates_m_and_aggregate_before_decision(self):
        rows = sample_klines(250)
        baseline = run_analysis(rows, index_klines=rows[-60:])
        result = run_analysis(rows, index_klines=rows[-60:], breadth={
            "up": 80, "down": 20, "total": 100, "breadth_ratio": 0.8, "source": "测试"})
        self.assertEqual(result.canslim.m_score, min(100, baseline.canslim.m_score + 15))
        self.assertGreater(result.canslim.total, baseline.canslim.total)
        self.assertEqual(result.module_scores["CAN_SLIM"], result.canslim.total)

    def test_breadth_refresh_boundary_does_not_drop_recent_scoring_snapshot(self):
        cache = MarketBreadthCache(lambda: {"total": 100})
        with patch("data.market_breadth.time.time", return_value=1000):
            cache.refresh()
        with patch("data.market_breadth.time.time", return_value=1121):
            snapshot = cache.snapshot()
            self.assertTrue(snapshot["stale"])
            self.assertTrue(snapshot["usable_for_scoring"])
        with patch("data.market_breadth.time.time", return_value=1301):
            self.assertFalse(cache.snapshot()["usable_for_scoring"])

    def test_sina_full_pool_counts_all_and_rejects_duplicate_or_missing_page(self):
        rows = [{"symbol": f"sh{i:06d}", "changepercent": 1 if i % 2 else -1} for i in range(150)]
        with patch.object(fetcher, "_fetch_sina_breadth_count", return_value=150), \
                patch.object(fetcher, "_fetch_sina_breadth_page", side_effect=lambda page: rows[(page-1)*100:page*100]):
            result = fetcher._collect_market_breadth_sina()
        self.assertEqual(result["up"], 75)
        self.assertEqual(result["down"], 75)
        self.assertEqual(result["total"], 150)
        with patch.object(fetcher, "_fetch_sina_breadth_count", return_value=150), \
                patch.object(fetcher, "_fetch_sina_breadth_page", return_value=rows[:100]):
            with self.assertRaises(ValueError):
                fetcher._collect_market_breadth_sina()
        with patch.object(fetcher, "_collect_market_breadth_sina", side_effect=OSError("新浪断开")), \
                patch.object(fetcher, "_collect_market_breadth_eastmoney", return_value={"total": 150, "source": "东方财富"}):
            self.assertEqual(fetcher.collect_market_breadth()["source"], "东方财富")
