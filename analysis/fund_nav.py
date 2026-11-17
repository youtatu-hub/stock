"""仅根据已披露净值及日涨跌幅计算基金趋势，不构造成交量或 OHLC。"""
from __future__ import annotations

import math
from datetime import date, timedelta

from analysis._indicators import sma_series

MA_PERIODS = (5, 20, 60)
RETURN_DAYS = {"week": 7, "month": 30, "quarter": 90}
# 来源日涨跌幅通常保留两位小数；常规日期使用净值比率，避免累积舍入误差。
GROWTH_ROUNDING_TOLERANCE = 0.005


def analyze_fund_nav(history: list[dict]) -> dict:
    # 倒推复权净值并对齐最新单位净值；缺少日涨跌幅时不跨越缺口推算。
    adjusted = [None] * len(history)
    adjusted[-1] = history[-1]["nav"]
    first = len(history) - 1
    adjustment_days = 0
    for i in range(len(history) - 1, 0, -1):
        change = history[i]["change_pct"]
        if change is None:
            break
        nav_ratio = history[i]["nav"] / history[i - 1]["nav"]
        if math.isclose((nav_ratio - 1) * 100, change, abs_tol=GROWTH_ROUNDING_TOLERANCE):
            ratio = nav_ratio
        else:
            ratio = 1 + change / 100
            adjustment_days += 1
        adjusted[i - 1] = adjusted[i] / ratio
        first = i - 1

    values = adjusted[first:]
    averages = {period: [None] * first + sma_series(values, period) for period in MA_PERIODS}
    latest = history[-1]
    latest_date = date.fromisoformat(latest["date"])
    returns = {}
    for name, days in RETURN_DAYS.items():
        cutoff = (latest_date - timedelta(days=days)).isoformat()
        baseline = next((i for i in range(len(history) - 1, first - 1, -1)
                         if history[i]["date"] <= cutoff), None)
        returns[name] = (round((values[-1] / adjusted[baseline] - 1) * 100, 2)
                         if baseline is not None else None)

    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, (value / peak - 1) * 100)
    ma20, ma60 = averages[20][-1], averages[60][-1]
    direction = "数据不足"
    if ma20 is not None and ma60 is not None:
        direction = ("上升" if values[-1] > ma20 > ma60 else
                     "下降" if values[-1] < ma20 < ma60 else "震荡")

    points = []
    for i, point in enumerate(history):
        points.append({**point, "adjusted_nav": adjusted[i],
                       **{f"ma{period}": series[i] for period, series in averages.items()}})
    return {"history": points, "latest": dict(latest), "analysis": {
        "direction": direction, "returns": returns,
        "ma20": ma20, "ma60": ma60,
        "max_drawdown_pct": round(drawdown, 2) if len(values) > 1 else None,
        "start_date": history[first]["date"], "end_date": latest["date"],
        "sample_count": len(values), "has_growth_gap": first > 0,
        "adjustment_days": adjustment_days,
    }}
