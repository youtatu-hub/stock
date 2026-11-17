"""同一行情快照中的价格与IOPV参考值；不混用联接基金净值。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .kline_fetcher import _cache_get, _cache_set, _http_get, utc_now
from .request_runtime import data_stage

PREMIUM_CACHE_SECONDS = 15
SNAPSHOT_FRESH_SECONDS = 120
CHINA_TZ = timezone(timedelta(hours=8))
SOURCE_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"


def _optional_number(value, name):
    if value in (None, "", "-"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name}不是有效数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}不是有效数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name}不是有限数值")
    return number


def parse_etf_snapshot(payload: dict, symbol: str) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("ETF参考值缺少行情对象")
    rows = payload["data"].get("diff") or []
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("f12") != symbol:
        raise ValueError("ETF参考值代码与请求不一致或数据为空")
    row = rows[0]
    price = _optional_number(row.get("f2"), "价格")
    iopv = _optional_number(row.get("f441"), "IOPV")
    if (price is not None and price <= 0) or (iopv is not None and iopv <= 0):
        raise ValueError("ETF价格和IOPV必须大于零")
    timestamp = _optional_number(row.get("f124"), "行情更新时间")
    if timestamp is None or timestamp <= 0:
        raise ValueError("ETF参考值缺少有效行情更新时间")
    try:
        stamp = datetime.fromtimestamp(timestamp, timezone.utc)
        data_date = datetime.strptime(str(row.get("f297")), "%Y%m%d").date()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("ETF参考值日期无效") from exc
    if stamp.astimezone(CHINA_TZ).date() != data_date:
        raise ValueError("ETF参考值行情时间与数据日期不一致")
    premium = (price / iopv - 1) * 100 if price is not None and iopv is not None else None
    discount = _optional_number(row.get("f402"), "来源折价率")
    if premium is not None and discount is not None and abs(premium + discount) > 0.1:
        raise ValueError("IOPV计算结果与来源折价率不一致")
    return {"symbol": symbol, "name": row.get("f14", ""), "price": price, "iopv": iopv,
            "premium_pct": round(premium, 3) if premium is not None else None,
            "data_date": data_date.isoformat(), "quote_time": stamp.isoformat(),
            "iopv_time": None, "source": "东方财富", "source_url": SOURCE_URL,
            "basis": "(同一来源快照价格 ÷ IOPV参考值 − 1) × 100%；正值为溢价，负值为折价。",
            "note": "来源未提供IOPV独立更新时间；跨境时差和汇率会影响参考值，偏离不等于可实现套利收益。"}


def snapshot_status(snapshot: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(snapshot["quote_time"])).total_seconds()
    fresh = 0 <= age <= SNAPSHOT_FRESH_SECONDS
    return {**snapshot, "snapshot_fresh": fresh,
            "status": "unavailable" if snapshot["premium_pct"] is None else "ok" if fresh else "stale",
            "age_seconds": max(0, round(age)),
            "fresh_until": (datetime.fromisoformat(snapshot["quote_time"])
                            + timedelta(seconds=SNAPSHOT_FRESH_SECONDS)).isoformat(),
            "alert_eligible": fresh and snapshot["premium_pct"] is not None}


@data_stage
def fetch_etf_premium(symbol: str) -> dict:
    cached = _cache_get(f"etf_premium_{symbol}", PREMIUM_CACHE_SECONDS)
    if cached is None:
        params = {"fltt": "2", "secids": f"{1 if symbol.startswith('5') else 0}.{symbol}",
                  "fields": "f12,f14,f2,f124,f297,f402,f441"}
        payload = _http_get(SOURCE_URL, params=params).json()
        cached = parse_etf_snapshot(payload, symbol)
        cached["fetched_at"] = utc_now()
        _cache_set(f"etf_premium_{symbol}", cached)
    return snapshot_status(cached)
