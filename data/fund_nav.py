"""场外基金已披露净值；只解析数据，不执行来源页面的 JavaScript。"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone

from data.kline_fetcher import _cache_get, _cache_set, _http_get, utc_now
from data.request_runtime import data_stage

NAV_CACHE_SECONDS = 300
NAV_HISTORY_COUNT = 500
CHINA_TZ = timezone(timedelta(hours=8))
NAV_SOURCE = "天天基金"


def _read_variable(source: str, name: str):
    match = re.search(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", source)
    if not match:
        raise ValueError(f"净值数据缺少 {name}")
    return json.JSONDecoder().raw_decode(source[match.end():])[0]


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"净值数据中的{label}无效")
    return float(value)


def parse_fund_nav(source: str, symbol: str) -> dict:
    if _read_variable(source, "fS_code") != symbol:
        raise ValueError("返回的基金代码与请求不一致")
    name = _read_variable(source, "fS_name")
    rows = _read_variable(source, "Data_netWorthTrend")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("净值数据缺少基金名称")
    if not isinstance(rows, list) or not rows:
        raise ValueError("该基金暂无已披露净值")

    history = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("基金净值记录格式无效")
        timestamp = _number(row.get("x"), "日期")
        nav = _number(row.get("y"), "单位净值")
        if nav <= 0:
            raise ValueError("单位净值必须大于零")
        growth = row.get("equityReturn")
        if growth is not None:
            growth = _number(growth, "日涨跌幅")
            if growth <= -100:
                raise ValueError("日涨跌幅必须大于 -100%")
        try:
            nav_date = datetime.fromtimestamp(timestamp / 1000, CHINA_TZ).date().isoformat()
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError("净值日期超出有效范围") from exc
        history.append({"date": nav_date, "nav": nav, "change_pct": growth})

    history.sort(key=lambda point: point["date"])
    if len({point["date"] for point in history}) != len(history):
        raise ValueError("净值数据存在重复日期")
    return {"symbol": symbol, "name": name.strip(), "history": history[-NAV_HISTORY_COUNT:]}


@data_stage
def fetch_fund_nav(symbol: str) -> dict:
    cache_key = f"fund_nav_{symbol}"
    cached = _cache_get(cache_key, NAV_CACHE_SECONDS)
    if cached is not None:
        return cached
    source_url = f"https://fund.eastmoney.com/pingzhongdata/{symbol}.js"
    response = _http_get(source_url)
    result = parse_fund_nav(response.content.decode("utf-8-sig"), symbol)
    result.update({"source": NAV_SOURCE, "source_url": source_url, "fetched_at": utc_now()})
    _cache_set(cache_key, result)
    return result
