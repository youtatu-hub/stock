"""同一行情快照中的价格与IOPV参考值；不混用联接基金净值。"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from .kline_fetcher import (
    TENCENT_QUOTE, _cache_get, _cache_set, _http_get, _source_error,
    requests, symbol_to_tencent, utc_now,
)
from .request_runtime import data_stage, deadline_scope

PREMIUM_CACHE_SECONDS = 15
PREMIUM_STALE_SECONDS = 300
PREMIUM_RETRY_SECONDS = 10
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
            "iopv_time": None, "iopv_time_status": "unprovided", "source": "东方财富", "source_url": SOURCE_URL,
            "basis": "(同一来源快照价格 ÷ IOPV参考值 − 1) × 100%；正值为溢价，负值为折价。",
            "note": "来源未提供IOPV独立更新时间，时效未核验；行情时间与获取时间均不能代替IOPV时间。参考偏离不触发实时提醒。"}


def parse_tencent_snapshot(text: str, symbol: str) -> dict:
    if '=\"' not in text:
        raise ValueError("腾讯未提供ETF报价")
    fields = text.split('=\"', 1)[1].split('"', 1)[0].split("~")
    if len(fields) < 83 or fields[2] != symbol or fields[82] != "CNY":
        raise ValueError("腾讯ETF代码、币种或参考值字段无效")
    stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=CHINA_TZ)
    premium = _optional_number(fields[77], "来源溢价率")
    # 腾讯ETF报价：77溢价率、78 IOPV、81已披露净值；不得把81冒充IOPV。
    result = parse_etf_snapshot({"data": {"diff": [{
        "f12": symbol, "f14": fields[1], "f2": fields[3], "f441": fields[78],
        "f402": -premium if premium is not None else None,
        "f124": stamp.timestamp(), "f297": stamp.strftime("%Y%m%d"),
    }]}}, symbol)
    result.update(source="腾讯", source_url=f"https://gu.qq.com/{symbol_to_tencent(symbol)}")
    return result


def snapshot_status(snapshot: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    quote_time = datetime.fromisoformat(snapshot["quote_time"])
    age = (now - quote_time).total_seconds()
    fresh = 0 <= age <= SNAPSHOT_FRESH_SECONDS
    iopv_time = snapshot.get("iopv_time")
    iopv_age = (now - datetime.fromisoformat(iopv_time)).total_seconds() if iopv_time else None
    iopv_fresh = iopv_age is not None and 0 <= iopv_age <= SNAPSHOT_FRESH_SECONDS
    usable = fresh and iopv_fresh and snapshot["iopv"] is not None and snapshot["premium_pct"] is not None
    if snapshot["iopv"] is None:
        reason, note = "value_missing", "来源未提供IOPV数值"
    elif snapshot["price"] is None:
        reason, note = "price_missing", "来源未提供比较用价格，不能计算当前IOPV折溢价"
    elif not iopv_time:
        reason, note = "valuation_time_missing", "来源没有独立估值时间，无法提供可核验的实时IOPV"
    elif not iopv_fresh:
        reason, note = "valuation_time_invalid", "IOPV估值时间已过期或在未来，不能用于当前折溢价"
    elif not fresh:
        reason, note = "quote_time_invalid", "比较用价格已过期或在未来，不能计算当前折溢价"
    else:
        reason, note = "", "价格和IOPV均有可核验的独立时间"
    oldest_time = min(quote_time, datetime.fromisoformat(iopv_time)) if iopv_time else quote_time
    return {**snapshot, "snapshot_fresh": fresh, "iopv_fresh": iopv_fresh,
            "status": "ok" if usable else "unavailable", "reason_code": reason, "note": note,
            "iopv": snapshot["iopv"] if usable else None,
            "premium_pct": snapshot["premium_pct"] if usable else None,
            "reported_reference": {"value": snapshot["iopv"], "valuation_time": iopv_time,
                                   "source": snapshot["source"], "source_url": snapshot["source_url"],
                                   "source_price": snapshot["price"], "price_time": snapshot["quote_time"],
                                   "fetched_at": snapshot.get("fetched_at"), "usable_for_analysis": usable},
            "age_seconds": max(0, round(age)),
            "fresh_until": (oldest_time + timedelta(seconds=SNAPSHOT_FRESH_SECONDS)).isoformat(),
            "alert_eligible": usable}


@data_stage
def fetch_etf_premium(symbol: str) -> dict:
    key = f"etf_premium_{symbol}"
    cached = _cache_get(key, PREMIUM_CACHE_SECONDS)
    if cached is not None:
        return snapshot_status(cached)
    previous = _cache_get(key, PREMIUM_STALE_SECONDS)
    error = _cache_get(key + "_error", PREMIUM_RETRY_SECONDS)
    if not error:
        params = {"fltt": "2", "secids": f"{1 if symbol.startswith('5') else 0}.{symbol}",
                  "fields": "f12,f14,f2,f124,f297,f402,f441"}
        errors = []
        sources = [("腾讯", TENCENT_QUOTE),
                   ("东方财富", "https://push2.eastmoney.com/api/qt/ulist.np/get"),
                   ("东方财富延迟节点", SOURCE_URL)]
        for source, url in sources:
            try:
                with deadline_scope(time.monotonic() + (3 if source == "腾讯" else 2)):
                    if source == "腾讯":
                        response = _http_get(url, params={"q": symbol_to_tencent(symbol)})
                        snapshot = parse_tencent_snapshot(response.content.decode("gb18030"), symbol)
                    else:
                        snapshot = parse_etf_snapshot(_http_get(url, params=params).json(), symbol)
                        snapshot.update(source=source, source_url=url)
                if snapshot["iopv"] is None:
                    raise ValueError("未提供IOPV参考值")
                if previous and snapshot["quote_time"] < previous["quote_time"]:
                    raise ValueError("返回的参考行情时间倒退")
                snapshot["fetched_at"] = utc_now()
                _cache_set(key, snapshot)
                return snapshot_status(snapshot)
            except (OSError, ValueError) as exc:
                reason = _source_error(exc) if isinstance(exc, requests.RequestException) else str(exc)
                errors.append(f"{source}：{reason}")
        error = "；".join(errors)
        _cache_set(key + "_error", error)
    if previous:
        result = snapshot_status(previous)
        result.update(status="unavailable", stale=True, iopv=None, premium_pct=None,
                      alert_eligible=False, refresh_error=error,
                      reason_code="refresh_failed", note="来源刷新失败，旧参考值不能用于当前IOPV折溢价")
        result["reported_reference"]["usable_for_analysis"] = False
        return result
    raise OSError(error)
