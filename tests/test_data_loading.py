from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import app
from data import kline_fetcher as fetcher
from data.market_breadth import MarketBreadthCache
from data.request_runtime import collect_stages, data_stage, deadline_scope

logging.disable(logging.CRITICAL)
BREADTH = {"up": 100, "down": 80, "flat": 20, "total": 200, "breadth_ratio": 0.556}


def sample_klines(count=60):
    return [fetcher.Kline(f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}", 10 + i / 10,
                         10.1 + i / 10, 10.3 + i / 10, 9.8 + i / 10, 10000 + i * 10)
            for i in range(count)]


class RuntimeTests(unittest.TestCase):
    def test_independent_stages_start_in_parallel(self):
        barrier = threading.Barrier(3)

        def task():
            barrier.wait(timeout=1)
            return [1]

        jobs = dict.fromkeys(("kline", "flow", "index"), task)
        values, errors, _ = collect_stages(jobs, required=("kline",), budget=1, optional_budget=1)
        self.assertEqual(set(values), set(jobs))
        self.assertFalse(errors)

    def test_optional_timeout_does_not_wait_for_worker_shutdown(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            values, errors, _ = collect_stages(
                {"kline": lambda: [1], "flow": lambda: release.wait(1)},
                required=("kline",), budget=0.4, optional_budget=0.05)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(values["kline"], [1])
            self.assertIn("flow", errors)
        finally:
            release.set()

    def test_required_timeout_and_network_failure_are_explicit(self):
        release = threading.Event()
        try:
            started = time.monotonic()
            values, errors, _ = collect_stages(
                {"kline": lambda: release.wait(1), "flow": Mock(side_effect=OSError("upstream down"))},
                required=("kline",), budget=0.06, optional_budget=0.02)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertFalse(values)
            self.assertIn("kline", errors)
            self.assertIn("upstream down", errors["flow"])
        finally:
            release.set()

    def test_programming_errors_are_not_silently_downgraded(self):
        with self.assertRaises(TypeError):
            collect_stages({"flow": Mock(side_effect=TypeError("bug"))})

    def test_concurrent_identical_requests_share_one_fetch(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        @data_stage
        def unique_fetch(symbol):
            calls.append(symbol)
            entered.set()
            release.wait(1)
            return [symbol]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(unique_fetch, "600000")
            self.assertTrue(entered.wait(1))
            others = [pool.submit(unique_fetch, "600000") for _ in range(3)]
            time.sleep(0.03)
            release.set()
            self.assertEqual([f.result() for f in [first, *others]], [["600000"]] * 4)
        self.assertEqual(calls, ["600000"])

    def test_real_http_slow_upstream_obeys_response_budget(self):
        release, entered = threading.Event(), threading.Event()

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                entered.set()
                release.wait(1)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(fetcher, "QUOTE_HOSTS", [f"http://127.0.0.1:{server.server_port}"]):
                started = time.monotonic()
                values, errors, _ = collect_stages({"quote": lambda: fetcher.fetch_quote("999998")},
                                                  required=("quote",), budget=0.08)
                self.assertTrue(entered.is_set())
                self.assertFalse(values)
                self.assertIn("quote", errors)
                self.assertLess(time.monotonic() - started, 0.4)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()


class BreadthTests(unittest.TestCase):
    def test_startup_read_never_fetches(self):
        source = Mock(return_value=BREADTH)
        cache = MarketBreadthCache(source)
        result = cache.snapshot()
        self.assertIsNone(result["data"])
        self.assertIsNone(result["updated_at"])
        self.assertTrue(result["stale"])
        source.assert_not_called()

    def test_failure_retains_old_result_and_timestamp(self):
        source = Mock(side_effect=[BREADTH, OSError("page timeout"), BREADTH])
        cache = MarketBreadthCache(source)
        cache.refresh()
        first = cache.snapshot()
        cache.refresh()
        failed = cache.snapshot()
        self.assertEqual(failed["data"], BREADTH)
        self.assertEqual(failed["updated_at"], first["updated_at"])
        self.assertEqual(failed["last_error"], "page timeout")
        self.assertTrue(failed["stale"])
        cache.refresh()
        self.assertFalse(cache.snapshot()["stale"])

    def test_expired_snapshot_returns_immediately(self):
        cache = MarketBreadthCache(lambda: BREADTH)
        cache.refresh()
        now = time.time()
        with patch("data.market_breadth.time.time", return_value=now + 121):
            self.assertTrue(cache.snapshot()["stale"])
            self.assertEqual(cache.snapshot()["data"], BREADTH)

    def test_snapshot_remains_available_during_refresh(self):
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(1)
            return BREADTH

        cache = MarketBreadthCache(slow)
        cache.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertTrue(cache.snapshot()["refreshing"])
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            release.set()
            cache.stop()

    def test_incomplete_pages_do_not_publish_success(self):
        rows = [{"f12": str(i), "f13": 1, "f3": 1} for i in range(100)]
        with patch.object(fetcher, "_fetch_breadth_page", side_effect=[
                {"total": 200, "diff": rows}, {"total": 200, "diff": rows[:50]}]):
            with self.assertRaises(ValueError):
                fetcher.collect_market_breadth()

    def test_first_page_is_fetched_once_and_all_rows_counted(self):
        def page(number):
            return {"total": 200, "diff": [
                {"f12": str(number * 100 + i), "f13": 1, "f3": 1 if number == 1 else -1}
                for i in range(100)]}
        with patch.object(fetcher, "_fetch_breadth_page", side_effect=page) as source:
            result = fetcher.collect_market_breadth()
        self.assertEqual(result["total"], 200)
        self.assertEqual(result["breadth_ratio"], 0.5)
        self.assertEqual(source.call_count, 2)


class ApiTests(unittest.TestCase):
    def test_analysis_degrades_optional_data_and_preserves_contract(self):
        klines = sample_klines()
        cache = MarketBreadthCache(lambda: BREADTH)
        cache.refresh()
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, "fetch_kline", return_value=klines))
            stack.enter_context(patch.object(app, "fetch_quote", return_value=None))
            stack.enter_context(patch.object(app, "fetch_fund_flow", side_effect=OSError("flow down")))
            stack.enter_context(patch.object(app, "fetch_index_kline", return_value=[]))
            stack.enter_context(patch.object(app, "fetch_kline_extras", return_value={}))
            stack.enter_context(patch.object(app, "market_breadth_cache", cache))
            result = app.handle_analyze({"symbol": ["600000"]})
        self.assertNotIn("error", result)
        self.assertIn("signal", result)
        self.assertEqual(result["breadth"], BREADTH)
        self.assertIn("fund_flow", result["degraded"])
        self.assertIn("fetch_total", result["timings_ms"])
        json.dumps(result, ensure_ascii=False)

    def test_missing_required_klines_is_not_success(self):
        with patch.object(app, "collect_stages", return_value=({}, {"kline": "timeout"}, {})):
            result = app.handle_analyze({"symbol": ["600000"]})
        self.assertEqual(result["error"], "timeout")
        self.assertNotIn("signal", result)

    def test_basic_kline_skips_enrichment(self):
        with patch.object(app, "fetch_kline", return_value=sample_klines()) as basic:
            with patch.object(app, "fetch_kline_extras") as extras:
                result = app.handle_kline({"symbol": ["600000"], "basic": ["1"]})
        self.assertTrue(result["basic"])
        self.assertFalse(basic.call_args.kwargs["enrich"])
        extras.assert_not_called()

    def test_enrichment_does_not_mutate_shared_price_cache(self):
        klines = sample_klines()
        enriched = fetcher.enrich_klines(klines, {klines[0].date: (1234, 2.5)})
        self.assertEqual(klines[0].amount, 0)
        self.assertEqual(enriched[0].amount, 1234)

    def test_weekly_extras_use_weekly_data(self):
        with patch.object(fetcher, "_get_json_eastmoney", return_value=None) as source:
            fetcher.fetch_kline_extras("999997", period="week")
        self.assertEqual(source.call_args.args[1]["klt"], "102")


if __name__ == "__main__":
    unittest.main()
