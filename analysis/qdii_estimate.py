"""以净值日收盘基准计算 QDII 模型估算，绝不使用报价自带的日涨跌幅。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


MAX_QUOTE_AGE_SECONDS = 120
NOTICE_THRESHOLD_PCT = 5
MODEL_NOTE = "模型误差尚未校准；期货基差与换月、离岸/在岸汇率差异、现金仓位和费用均可能造成偏差。"
FORMULA = "已披露净值 × (期货现价 / 净值日收盘期货价) × (USD/CNH现价 / 净值日收盘USD/CNH)"


def positive_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为正数")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}必须为正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须为有限正数")
    return number


def aware_time(value, label):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}无效") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{label}必须包含时区")
    return stamp


def fresh_quote(quote, now, label):
    if not isinstance(quote, dict):
        raise ValueError(f"缺少{label}")
    price = positive_number(quote.get("price"), label)
    stamp = aware_time(quote.get("time"), f"{label}时间")
    if quote.get("stale") or not 0 <= (now - stamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS:
        raise ValueError(f"{label}已过期或时间在未来")
    return price, stamp


def unavailable_estimate(reason, note, *, nav_date=None):
    return {"status": "unavailable", "kind": "model_estimate", "label": "模型估算",
            "reason_code": reason, "note": note, "nav_date": nav_date,
            "est_nav": None, "est_nav_time": None, "est_premium_pct": None,
            "alert_eligible": False, "uncertainty_note": MODEL_NOTE}


def calculate_estimate(nav, baseline, market, *, future_symbol, quote=None, now=None):
    now = now or datetime.now(timezone.utc)
    if baseline["nav_date"] != nav["date"]:
        raise ValueError("基准日期与实际净值日期不一致")
    close = aware_time(baseline["close_time"], "净值日收盘时间")
    if close > now:
        raise ValueError("净值日收盘基准在未来")
    nav_value = positive_number(nav["nav"], "基准净值")
    future = market[future_symbol]
    fx = market["USD/CNH"]
    base_future = baseline["quotes"][future_symbol]
    base_fx = baseline["quotes"]["USD/CNH"]
    for current, base in ((future, base_future), (fx, base_fx)):
        if current["series"] != base["series"] or current["price_basis"] != base["price_basis"]:
            raise ValueError("当前报价与收盘基准的品种或价格口径不一致")
    future_price, future_time = fresh_quote(future, now, f"{future_symbol}期货报价")
    fx_price, fx_time = fresh_quote(fx, now, "USD/CNH即期报价")
    if min(future_time, fx_time) < close:
        raise ValueError("当前报价早于净值日收盘基准")
    future_ratio = future_price / positive_number(base_future["price"], "期货基准价")
    fx_ratio = fx_price / positive_number(base_fx["price"], "汇率基准价")
    estimate = positive_number(nav_value * future_ratio * fx_ratio, "估算净值")
    price = price_time = premium = None
    premium_note = "市价缺失或过期，未计算估算折溢价"
    if quote is not None:
        try:
            price, price_time = fresh_quote(quote, now, "ETF市价")
        except ValueError as exc:
            premium_note = str(exc) + "，未计算估算折溢价"
        else:
            premium = (price / estimate - 1) * 100
            if not math.isfinite(premium):
                raise ValueError("估算折溢价不是有限数值")
            premium_note = "估算折溢价仅供参考，不触发交易提醒"
    oldest = min(future_time, fx_time)
    expiry = min(oldest, price_time) if price_time else oldest
    return {
        "status": "ok", "kind": "model_estimate", "label": "模型估算",
        "est_nav": round(estimate, 6), "est_nav_time": oldest.isoformat(),
        "est_premium_pct": round(premium, 3) if premium is not None else None,
        "price": price, "price_time": price_time.isoformat() if price_time else None,
        "premium_note": premium_note, "future_symbol": future_symbol,
        "nav_date": nav["date"], "base_nav": nav_value, "baseline_close_time": close.isoformat(),
        "baseline_origin": baseline["origin"], "baseline_source": baseline["source"],
        "inputs": {"future": future, "fx": fx, "base_future": base_future, "base_fx": base_fx},
        "future_ratio": future_ratio, "fx_ratio": fx_ratio, "formula": FORMULA,
        "fresh_until": (expiry + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
        "calculated_at": now.isoformat(), "alert_eligible": False,
        "notice_threshold_pct": NOTICE_THRESHOLD_PCT,
        "notice": premium is not None and abs(premium) >= NOTICE_THRESHOLD_PCT,
        "uncertainty_note": MODEL_NOTE,
    }
