"""资金流响应、失败冷却和最近成功快照；独立于报价的请求队列。"""
from __future__ import annotations

import concurrent.futures
import copy
import math
import threading
import time
import uuid
from datetime import datetime

from data.kline_fetcher import utc_now
from data.market_time import flow_session_status
from data.request_runtime import collect_stages, data_stage

FLOW_BUDGET_SECONDS = 8.0
FLOW_REFRESH_SECONDS = 10
FLOW_RETRY_SECONDS = 30
FLOW_SNAPSHOT_SECONDS = 300
MAX_ANALYSIS_SNAPSHOTS = 2048
FLOW_AMOUNT_FIELDS = ("main_net", "super_large_net", "large_net", "medium_net", "small_net")
_flow_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="flow-request")


def history_status(flows, klines=(), period="day", *, requested=True) -> dict:
    latest = max(flows, key=lambda item: item.date) if flows else None
    reference = klines[-1].date if klines else None
    return {
        "scope": "daily_history", "requested": requested,
        "status": "not_requested" if not requested else "available" if flows else "unavailable",
        "availability_note": "日级历史未请求" if not requested else "日级历史可用" if flows else "日级历史未取得",
        "source": latest.source if latest else "东方财富", "available": bool(flows) if requested else None,
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


def minute_coverage(flows) -> dict:
    missing, previous = [], None
    for row in flows:
        if row.available:
            previous = None
            continue
        stamp = datetime.fromisoformat(f"{row.date}T{row.time}")
        if (previous is not None and (stamp - previous).total_seconds() == 60
                and missing[-1]["reason"] == row.quality_note):
            missing[-1]["end"] = row.time
            missing[-1]["count"] += 1
        else:
            missing.append({"date": row.date, "start": row.time, "end": row.time,
                            "count": 1, "reason": row.quality_note})
        previous = stamp
    missing_count = sum(item["count"] for item in missing)
    return {"minute_status": "unavailable" if missing_count == len(flows) else "partial" if missing else "complete",
            "missing_count": missing_count, "missing_ranges": missing}


def analysis_snapshot(payload: dict) -> dict:
    """只裁剪分钟明细；页面展示的金额、状态、时间和质量信息原样交给AI。"""
    return copy.deepcopy({key: value for key, value in payload.items()
                          if key not in ("flows", "retry_after_seconds")})


def availability_note(payload: dict) -> str:
    if not payload["summary_available"]:
        cumulative = "当前累计值不可用"
    elif payload.get("stale"):
        cumulative = "累计值为旧快照"
    else:
        cumulative = "实时累计可用" if payload.get("is_today") else "最近交易日累计可用"
    minute = {"complete": "分钟序列完整", "partial": "分钟序列局部缺失",
              "unavailable": "分钟序列不可用"}[payload["minute_status"]]
    return f"{cumulative}；{minute}"


class RealtimeFlowService:
    def __init__(self, budget=FLOW_BUDGET_SECONDS):
        self.budget = budget
        self._lock = threading.Lock()
        self._states = {}
        self._analysis_snapshots = {}

    def _remember_snapshot(self, payload: dict) -> None:
        """同一状态保存轻量历史，页面刷新后仍可读取AI请求引用的那一版。调用方持锁。"""
        now = time.monotonic()
        expired = [key for key, (created, _) in self._analysis_snapshots.items()
                   if now - created > FLOW_SNAPSHOT_SECONDS]
        for key in expired:
            del self._analysis_snapshots[key]
        if len(self._analysis_snapshots) >= MAX_ANALYSIS_SNAPSHOTS:
            self._analysis_snapshots.pop(next(iter(self._analysis_snapshots)))
        self._analysis_snapshots[payload["snapshot_id"]] = (now, analysis_snapshot(payload))

    def for_analysis(self, symbol: str, reference: dict) -> dict:
        if not isinstance(reference, dict) or set(reference) - {"id", "status"}:
            raise ValueError("资金流快照引用无效")
        snapshot_id, status = reference.get("id"), reference.get("status")
        if status not in ("ok", "partial", "stale", "unavailable", "error"):
            raise ValueError("资金流展示状态无效")
        if snapshot_id is None and status in ("error", "unavailable"):
            return {"symbol": symbol, "scope": "realtime", "snapshot_id": None,
                    "status": status, "summary_available": False, "summary": {},
                    "minute_status": "unavailable", "missing_count": 0, "missing_ranges": [], "as_of": None,
                    "availability_note": "当前累计值不可用；分钟序列不可用",
                    "message": "页面未取得实时资金流快照，不能判断当前累计净额"}
        if not isinstance(snapshot_id, str) or len(snapshot_id) != 32:
            raise ValueError("资金流快照编号无效")
        with self._lock:
            stored = self._analysis_snapshots.get(snapshot_id)
            if not stored or time.monotonic() - stored[0] > FLOW_SNAPSHOT_SECONDS:
                raise ValueError("资金流快照已过期，请刷新资金流后再生成解读")
            result = copy.deepcopy(stored[1])
        if result["symbol"] != symbol:
            raise ValueError("资金流快照与分析品种不一致")
        if status == "stale":
            result.update(status="stale", stale=True,
                          message="页面最近一次刷新失败，仍展示此快照的历史累计值")
            result["availability_note"] = availability_note(result)
        elif status != result["status"]:
            raise ValueError("资金流快照与页面状态不一致，请刷新后重试")
        return result

    @data_stage
    def snapshot(self, symbol: str, fetcher, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            previous = self._states.get(symbol)
            if previous and not force and now < previous["next_attempt"]:
                payload = previous["payload"]
                session = flow_session_status(payload.get("data_date"))
                if any(payload.get(key) != value for key, value in session.items()):
                    payload = {**payload, **session, "snapshot_id": uuid.uuid4().hex}
                    payload["availability_note"] = availability_note(payload)
                    self._remember_snapshot(payload)
                    previous["payload"] = payload
                return {**payload, "retry_after_seconds": max(1, math.ceil(previous["next_attempt"] - now))}
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
            coverage = minute_coverage(flows)
            payload = {
                "symbol": symbol, "status": "partial" if any(not row.available for row in flows) else "ok", "stale": False,
                "scope": "realtime", "summary_available": last.available, **coverage,
                "source": last.source, "checked_at": checked_at,
                "source_note": f"按{last.source}口径统计，不同来源的资金流金额可能有差异",
                "accounting_note": "主力净额 = 超大单净额 + 大单净额",
                "quality_note": "；".join(dict.fromkeys(flow.quality_note for flow in flows if flow.quality_note)),
                "fetched_at": last.fetched_at or checked_at,
                "data_date": last.date or None,
                "as_of": f"{last.date} {last.time}".strip(),
                "flows": [{"time": flow.time, "available": flow.available,
                           "missing_reason": flow.quality_note if not flow.available else "",
                           **{key: getattr(flow, key) if flow.available else None for key in
                              FLOW_AMOUNT_FIELDS}} for flow in flows],
                "summary": {key: getattr(last, key) if last.available else None for key in
                            FLOW_AMOUNT_FIELDS},
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
                           "scope": "realtime", "summary_available": False, "summary": {},
                           "minute_status": "unavailable", "missing_count": 0, "missing_ranges": [],
                           "flows": [], "source": "腾讯 / 东方财富", "message": reason,
                           "checked_at": checked_at, "fetched_at": None, "as_of": None}
        payload.update(flow_session_status(payload.get("data_date")))
        payload["availability_note"] = availability_note(payload)
        payload["retry_after_seconds"] = retry_seconds
        payload["snapshot_id"] = uuid.uuid4().hex
        with self._lock:
            self._remember_snapshot(payload)
            if len(self._states) >= 512 and symbol not in self._states:
                self._states.pop(next(iter(self._states)))
            self._states[symbol] = {"payload": payload, "good": good, "good_at": good_at,
                                    "next_attempt": time.monotonic() + retry_seconds}
        return payload


realtime_flow_service = RealtimeFlowService()
