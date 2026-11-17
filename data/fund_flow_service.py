"""资金流响应、失败冷却和最近成功快照；独立于报价的请求队列。"""
from __future__ import annotations

import concurrent.futures
import math
import threading
import time

from data.kline_fetcher import utc_now
from data.market_time import flow_session_status
from data.request_runtime import collect_stages, data_stage

FLOW_BUDGET_SECONDS = 8.0
FLOW_REFRESH_SECONDS = 10
FLOW_RETRY_SECONDS = 30
FLOW_SNAPSHOT_SECONDS = 300
_flow_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="flow-request")


def history_status(flows, klines=(), period="day") -> dict:
    latest = max(flows, key=lambda item: item.date) if flows else None
    reference = klines[-1].date if klines else None
    return {
        "source": latest.source if latest else "东方财富", "available": bool(flows),
        "data_through": latest.date if latest else None,
        "fetched_at": latest.fetched_at if latest else None,
        "reference_date": reference,
        "lagging": bool(latest and reference and latest.date < reference),
        "lag_trading_days": sum(k.date > latest.date for k in klines)
        if latest and period == "day" else None,
        "period": period,
    }


def serialize_history(flows) -> list:
    return [{"date": flow.date, "main_net": flow.main_net,
             "super_large_net": flow.super_large_net, "large_net": flow.large_net,
             "medium_net": flow.medium_net, "small_net": flow.small_net,
             "source": flow.source, "main_pct": flow.main_pct} for flow in flows]


class RealtimeFlowService:
    def __init__(self, budget=FLOW_BUDGET_SECONDS):
        self.budget = budget
        self._lock = threading.Lock()
        self._states = {}

    @data_stage
    def snapshot(self, symbol: str, fetcher, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            previous = self._states.get(symbol)
            if previous and not force and now < previous["next_attempt"]:
                return {**previous["payload"], **flow_session_status(previous["payload"].get("data_date")), "retry_after_seconds":
                        max(1, math.ceil(previous["next_attempt"] - now))}
        values, errors, _ = collect_stages(
            {"realtime_flow": lambda: {"flows": fetcher(symbol)}},
            required=("realtime_flow",), budget=self.budget,
            executor=_flow_executor,
        )
        checked_at = utc_now()
        flows = values.get("realtime_flow", {}).get("flows") or []
        message = errors.get("realtime_flow", "")
        good = previous.get("good") if previous else None
        good_at = previous.get("good_at", 0) if previous else 0
        if flows:
            last = flows[-1]
            payload = {
                "symbol": symbol, "status": "ok", "stale": False,
                "source": last.source, "checked_at": checked_at,
                "source_note": f"按{last.source}口径统计，不同来源的资金流金额可能有差异",
                "accounting_note": "主力净额 = 超大单净额 + 大单净额；各分项可一进一出，不能只比较主力与超大单",
                "fetched_at": last.fetched_at or checked_at,
                "data_date": last.date or None,
                "as_of": f"{last.date} {last.time}".strip(),
                "flows": [{"time": flow.time, "main_net": flow.main_net,
                           "super_large_net": flow.super_large_net, "large_net": flow.large_net,
                           "medium_net": flow.medium_net, "small_net": flow.small_net} for flow in flows],
                "summary": {"main_net": last.main_net, "super_large_net": last.super_large_net,
                            "large_net": last.large_net, "medium_net": last.medium_net,
                            "small_net": last.small_net},
                "time_range": f"{flows[0].time} ~ {last.time}",
            }
            good, good_at = payload, time.monotonic()
            retry_seconds = FLOW_REFRESH_SECONDS
        else:
            retry_seconds = FLOW_RETRY_SECONDS
            reason = message or "数据源未提供该品种的分时资金流记录，原因未提供"
            if good and time.monotonic() - good_at <= FLOW_SNAPSHOT_SECONDS:
                payload = {**good, "status": "stale", "stale": True,
                           "message": reason, "checked_at": checked_at}
            else:
                good = None
                payload = {"symbol": symbol, "status": "error" if message else "unavailable",
                           "flows": [], "source": "腾讯 / 东方财富", "message": reason,
                           "checked_at": checked_at, "fetched_at": None, "as_of": None}
        payload.update(flow_session_status(payload.get("data_date")))
        payload["retry_after_seconds"] = retry_seconds
        with self._lock:
            if len(self._states) >= 512 and symbol not in self._states:
                self._states.pop(next(iter(self._states)))
            self._states[symbol] = {"payload": payload, "good": good, "good_at": good_at,
                                    "next_attempt": time.monotonic() + retry_seconds}
        return payload


realtime_flow_service = RealtimeFlowService()
