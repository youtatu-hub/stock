"""按已核对的美股交易日历确定净值日收盘时刻，不用北京时间日期猜测。"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@lru_cache(maxsize=1)
def session_calendar():
    path = Path(__file__).with_name("us_stock_sessions.json")
    return json.loads(path.read_text(encoding="utf-8"))


def new_york_zone():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise ValueError("缺少 America/New_York 时区数据，请安装 tzdata") from exc


def us_stock_close(day: date) -> datetime | None:
    calendar = session_calendar()
    year = calendar["years"].get(str(day.year))
    if year is None:
        raise ValueError(f"尚未配置 {day.year} 年美股收盘日历")
    if day.weekday() >= 5 or day.isoformat() in year["holidays"]:
        return None
    close_time = year["early_closes"].get(day.isoformat(), calendar["regular_close"])
    return datetime.combine(day, time.fromisoformat(close_time), new_york_zone()).astimezone(timezone.utc)


def next_us_stock_close(now: datetime) -> datetime:
    day = now.astimezone(new_york_zone()).date()
    for offset in range(15):
        close = us_stock_close(day + timedelta(days=offset))
        if close is not None and close > now:
            return close
    raise ValueError("未来两周未找到美股收盘时刻，请核对交易日历")
