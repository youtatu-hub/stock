"""市场宽度只由后台刷新，前台读取最近一次完整成功的快照。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from data.kline_fetcher import collect_market_breadth
from data.request_runtime import deadline_scope, request_id

log = logging.getLogger("trend_data")
REFRESH_INTERVAL_SECONDS = 120
REFRESH_BUDGET_SECONDS = 30
MAX_SCORING_AGE_SECONDS = 300


def _iso_timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value else None


class MarketBreadthCache:
    def __init__(self, fetcher=collect_market_breadth, interval=REFRESH_INTERVAL_SECONDS):
        self._fetcher = fetcher
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._data = None
        self._updated_at = None
        self._last_attempt_at = None
        self._last_error = None
        self._refreshing = False

    def snapshot(self) -> dict:
        with self._lock:
            age = max(0, time.time() - self._updated_at) if self._updated_at else None
            return {
                "data": dict(self._data) if self._data else None,
                "available": self._data is not None,
                "updated_at": _iso_timestamp(self._updated_at),
                "age_seconds": round(age, 1) if age is not None else None,
                "stale": age is None or age >= self._interval or self._last_error is not None,
                "usable_for_scoring": age is not None and age <= MAX_SCORING_AGE_SECONDS,
                "refreshing": self._refreshing,
                "last_attempt_at": _iso_timestamp(self._last_attempt_at),
                "last_error": self._last_error,
                "refresh_interval_seconds": self._interval,
            }

    def refresh(self):
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._last_attempt_at = time.time()
        started = time.monotonic()
        token = request_id.set("breadth-refresh")
        try:
            with deadline_scope(started + REFRESH_BUDGET_SECONDS):
                result = self._fetcher()
            if not result or result.get("total", 0) < 100:
                raise ValueError("市场宽度数据不完整")
            with self._lock:
                self._data = dict(result)
                self._updated_at = time.time()
                self._last_error = None
        except Exception as exc:
            # 后台任务边界：保留成功快照，明确暴露失败，下一周期重试。
            with self._lock:
                self._last_error = str(exc)
            log.exception("市场宽度刷新失败，保留最近成功结果")
        finally:
            with self._lock:
                self._refreshing = False
            log.info("data_stage request_id=breadth-refresh stage=market_breadth elapsed_ms=%.1f status=%s",
                     (time.monotonic() - started) * 1000, self.snapshot()["last_error"] or "ok")
            request_id.reset(token)

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self.refresh()
            self._stop.wait(max(0, self._interval - (time.monotonic() - started)))

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="market-breadth", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


market_breadth_cache = MarketBreadthCache()
