"""按北京时间解释行情日期；不把工作日自动当作已开市的交易日。"""
from datetime import datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def flow_session_status(data_date=None, now=None):
    moment = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = moment.date().isoformat()
    is_today = data_date == today
    if is_today:
        state, note = "today", "今日实时，数值为当日累计（截至数据时间）"
    elif moment.weekday() >= 5:
        state, note = "weekend", "今日为周末"
    elif moment.time() < time(9, 30):
        state, note = "preopen", "今日尚未进入连续交易（09:30开始）"
    else:
        state, note = "awaiting_today", "数据源尚未返回今日记录，不能视为今日盘中数据"
    if not is_today:
        note += f"，显示最近可用交易日 {data_date} 的数据" if data_date else "，暂无可用分时记录"
    return {"market_date": today, "is_today": is_today, "session_state": state,
            "session_note": note}
