"""独立 RQData IOPV 核验；仅打印报告，不写行情、不修改业务状态。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


SYMBOLS = ("513300.XSHG", "513500.XSHG", "513100.XSHG", "159501.XSHE", "159941.XSHE")
CHINA_TZ = timezone(timedelta(hours=8))
MAX_AGE_SECONDS = 15 * 60
API_TIMEOUT_SECONDS = 15
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(15)))
EXIT_CODES = {"probe_pass": 0, "probe_fail": 1, "error": 1, "not_ready": 2, "needs_review": 2}


def environment_status():
    return {
        "python": sys.executable,
        "sdk_installed": importlib.util.find_spec("rqdatac") is not None,
        "environment_configured": any(os.environ.get(key) for key in ("RQDATAC_CONF", "RQDATAC2_CONF")),
    }


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def local_timestamp(value):
    # RQData 中国市场的无时区时间按北京时间解释，不用本机时区猜测。
    stamp = datetime.fromisoformat(str(value))
    return stamp.replace(tzinfo=CHINA_TZ) if stamp.tzinfo is None else stamp.astimezone(CHINA_TZ)


def load_halt_calendar(path):
    """仅接受带来源说明、明确列出五只 ETF 的已核实停牌区间。"""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("停牌日历必须是按 YYYY-MM-DD 分组的对象")
    calendar = {}
    for day_text, entry in payload.items():
        day = date.fromisoformat(day_text)
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or not entry["source"].strip():
            raise ValueError("每个停牌核验日期必须有 source 来源说明")
        symbols = entry.get("symbols")
        if not isinstance(symbols, dict) or set(symbols) != set(SYMBOLS):
            raise ValueError("每个日期必须明确列出五只 ETF；已核实无停牌填空数组")
        parsed = {}
        for symbol, periods in symbols.items():
            if not isinstance(periods, list):
                raise ValueError("停牌区间必须是数组")
            parsed[symbol] = []
            for period in periods:
                if not isinstance(period, list) or len(period) != 2:
                    raise ValueError("停牌区间格式必须为 [开始时间, 复牌时间]")
                start, end = [datetime.combine(day, time.fromisoformat(value), CHINA_TZ) for value in period]
                if not time(9, 30) <= start.time() < end.time() <= time(15):
                    raise ValueError("停牌区间必须在 09:30 至 15:00 内，结束时间须晚于开始时间")
                parsed[symbol].append((start, end))
            parsed[symbol].sort()
            if any(left[1] > right[0] for left, right in zip(parsed[symbol], parsed[symbol][1:])):
                raise ValueError("同一品种的停牌区间不能重叠")
        calendar[day] = {"source": entry["source"], "symbols": parsed}
    return calendar


def trading_intervals(day, halts):
    intervals = [(datetime.combine(day, start, CHINA_TZ), datetime.combine(day, end, CHINA_TZ))
                 for start, end in SESSIONS]
    for halt_start, halt_end in halts:
        remaining = []
        for start, end in intervals:
            if halt_end <= start or halt_start >= end:
                remaining.append((start, end))
                continue
            if start < halt_start:
                remaining.append((start, halt_start))
            if halt_end < end:
                remaining.append((halt_end, end))
        intervals = remaining
    return intervals


def interval_index(stamp, intervals, halts):
    if any(start <= stamp < end for start, end in halts):
        return None
    return next((index for index, (start, end) in enumerate(intervals) if start <= stamp <= end), None)


def snapshot_report(tick, started_at, received_at, trading_day, halts, max_age, *, halt_verified=False):
    value = positive_number(getattr(tick, "iopv", None))
    price = positive_number(getattr(tick, "last", None))
    raw_time = getattr(tick, "datetime", None)
    try:
        stamp = local_timestamp(raw_time)
    except (ValueError, TypeError):
        stamp = None
    intervals = trading_intervals(received_at.date(), halts)
    current_interval = interval_index(received_at, intervals, halts)
    same_session = stamp is not None and current_interval is not None and (
        interval_index(stamp, intervals, halts) == current_interval)
    age = (received_at - stamp).total_seconds() if stamp else None
    phase = getattr(tick, "trading_phase_code", None)
    phase = str(phase).strip() if phase is not None else None
    halted = phase in ("H", "P") or any(start <= received_at < end for start, end in halts)
    time_ok = trading_day and same_session and age is not None and 0 <= age <= max_age
    return {
        "iopv": value, "price": price, "prev_iopv": positive_number(getattr(tick, "prev_iopv", None)),
        "tick_time": stamp.isoformat() if stamp else None,
        "request_started_at": started_at.isoformat(), "received_at": received_at.isoformat(),
        "request_seconds": round((received_at - started_at).total_seconds(), 3),
        "tick_age_seconds": round(age, 3) if age is not None else None,
        "trading_phase_code": phase, "halted": halted,
        "value_present": value is not None and price is not None,
        "time_within_threshold": bool(time_ok),
        "usable_sample": bool(value is not None and price is not None and time_ok and not halted and halt_verified),
    }


def history_report(records, symbol, day, halts, max_age):
    intervals = trading_intervals(day, halts)
    observed = [[] for _ in intervals]
    values, changes = [], []
    invalid = excluded = regressions = 0
    previous_stamp = None
    previous_value = None
    for row in records:
        if row.get("order_book_id") != symbol:
            raise ValueError(f"{symbol} 历史响应包含其他品种或缺少代码")
        stamp = local_timestamp(row["datetime"])
        if stamp.date() != day:
            raise ValueError(f"{symbol} 历史响应日期与请求不一致")
        if previous_stamp is not None and stamp < previous_stamp:
            regressions += 1
        previous_stamp = stamp
        interval = interval_index(stamp, intervals, halts)
        if interval is None:
            excluded += 1
            continue
        observed[interval].append(stamp)
        value = positive_number(row.get("iopv"))
        if value is None or positive_number(row.get("last")) is None:
            invalid += 1
            previous_value = None
            continue
        values.append((stamp, value))
        if previous_value is not None and value != previous_value:
            changes.append(stamp)
        previous_value = value
    gaps = []
    for (start, end), stamps in zip(intervals, observed):
        points = [start, *sorted(stamps), end]
        gaps.extend((right - left).total_seconds() for left, right in zip(points, points[1:]))
    maximum_gap = max(gaps, default=0)
    distinct = len({value for _, value in values})
    prices = [value for _, value in values]
    return {
        "date": day.isoformat(), "record_count": len(records), "valid_count": len(values),
        "invalid_count": invalid, "excluded_count": excluded, "time_regressions": regressions,
        "first_tick_time": values[0][0].isoformat() if values else None,
        "last_tick_time": values[-1][0].isoformat() if values else None,
        "last_iopv": prices[-1] if prices else None, "distinct_iopv_count": distinct,
        "change_count": len(changes),
        "first_change_time": changes[0].isoformat() if changes else None,
        "last_change_time": changes[-1].isoformat() if changes else None,
        "min_iopv": min(prices) if prices else None, "max_iopv": max(prices) if prices else None,
        "range_pct": round((max(prices) / min(prices) - 1) * 100, 6) if prices else None,
        "max_uncovered_seconds": maximum_gap,
        "full_day_covered": bool(values and intervals and maximum_gap <= max_age and not regressions),
        "intraday_changed": distinct > 1,
    }


def history_records(frame):
    if frame is None:
        return []
    # datetime 是索引；不把它作为 get_price 的值字段请求。
    table = frame.reset_index()
    required = {"order_book_id", "datetime", "last", "iopv"}
    if not required.issubset(table.columns):
        raise ValueError("RQData 历史 tick 缺少代码、时间、last 或 iopv 字段")
    return table.to_dict("records")


def run_probe(client, *, history_day=None, halt_calendar=None, max_age=MAX_AGE_SECONDS,
              clock=lambda: datetime.now(CHINA_TZ)):
    calendar = halt_calendar or {}
    now = clock()
    days = client.get_trading_dates(now.date() - timedelta(days=60), now.date(), market="cn")
    days = sorted({date.fromisoformat(str(day)[:10]) for day in days})
    complete_days = [day for day in days if day < now.date()]
    if not complete_days:
        raise ValueError("未取得最近完整交易日")
    previous_day = complete_days[-1]
    day = history_day or previous_day
    if day not in complete_days:
        raise ValueError("--date 必须是今天之前的已完成交易日，且在最近 60 天内")
    started_at = clock()
    snapshots = client.current_snapshot(list(SYMBOLS), market="cn")
    received_at = clock()
    if received_at.date() != now.date():
        raise ValueError("请求跨越交易日期，请重新采样")
    if not isinstance(snapshots, (list, tuple)):
        raise ValueError("RQData 多品种快照未返回 Tick 列表")
    by_symbol = {getattr(tick, "order_book_id", None): tick for tick in snapshots}
    if len(by_symbol) != len(snapshots) or not set(by_symbol).issubset(SYMBOLS):
        raise ValueError("RQData 快照包含重复、未知或缺失的品种代码")
    current_is_trading_day = received_at.date() in days
    market_open = current_is_trading_day and interval_index(
        received_at, trading_intervals(received_at.date(), []), []) is not None
    results = []
    reviewed = day in calendar and received_at.date() in calendar
    for symbol in SYMBOLS:
        live_halts = calendar.get(received_at.date(), {}).get("symbols", {}).get(symbol, [])
        history_halts = calendar.get(day, {}).get("symbols", {}).get(symbol, [])
        live = snapshot_report(by_symbol.get(symbol), started_at, received_at,
                               current_is_trading_day, live_halts, max_age,
                               halt_verified=received_at.date() in calendar)
        frame = client.get_price(symbol, start_date=day, end_date=day, frequency="tick",
                                 fields=["last", "iopv"], adjust_type="none", skip_suspended=True,
                                 expect_df=True, market="cn")
        history = history_report(history_records(frame), symbol, day, history_halts, max_age)
        previous_matches = None
        if day == previous_day and live["prev_iopv"] is not None and history["last_iopv"] is not None:
            previous_matches = math.isclose(live["prev_iopv"], history["last_iopv"], rel_tol=0, abs_tol=1e-8)
        checks = {
            "live_value": live["value_present"], "live_time": live["time_within_threshold"],
            "not_halted": not live["halted"], "history_changed": history["intraday_changed"],
            "history_complete": history["full_day_covered"] and history["invalid_count"] == 0,
        }
        results.append({"symbol": symbol, "snapshot": live, "history": history, "checks": checks,
                        "prev_iopv_matches_previous_day_last": previous_matches})
    checks_passed = all(all(item["checks"].values()) for item in results)
    previous_values_reviewed = all(item["prev_iopv_matches_previous_day_last"] is True for item in results)
    status = "probe_pass" if checks_passed else "probe_fail"
    if not market_open:
        status = "not_ready"
    elif not reviewed or (checks_passed and not previous_values_reviewed):
        status = "needs_review"
    return {
        "status": status, "source": "RQData", "history_date": day.isoformat(),
        "max_age_seconds": max_age, "market_open": market_open,
        "numeric_checks_passed": checks_passed, "suspensions_reviewed": reviewed,
        "halt_sources": {key.isoformat(): value["source"] for key, value in calendar.items()},
        "item_19_complete": False,
        "notes": [
            "tick_time 是交易所 tick 时间；本探针不将其写入业务 iopv_time。",
            "15 分钟为本次探针门槛，报告保留实际秒数；该门槛不能证明是无延迟的实时权限。",
            "prev_iopv 与前一交易日末值仅作数值交叉检查，字段语义和精度仍需来源确认。",
            "无变化按本次要求不通过数值筛选，但不能仅据此推断官方数据陈旧。",
            "probe_pass 只代表采样筛选通过；估值口径、时间语义、权限及完整交易日接入验收仍需核实。",
        ],
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="完整交易日 YYYY-MM-DD；默认上一个交易日")
    parser.add_argument("--max-age-seconds", type=int, default=MAX_AGE_SECONDS, help="探针时效门槛，默认 900 秒")
    parser.add_argument("--halt-calendar", type=Path, help="已核实的停复牌区间 JSON；缺少时不能判通过")
    parser.add_argument("--check-env", action="store_true", help="只检查 SDK 和环境配置，不联网")
    args = parser.parse_args(argv)
    if not 1 <= args.max_age_seconds <= MAX_AGE_SECONDS:
        parser.error("--max-age-seconds 必须在 1 到 900 秒之间")
    environment = environment_status()
    if args.check_env or not environment["sdk_installed"]:
        report = {"status": "not_ready", "environment": environment, "item_19_complete": False,
                  "reason": "仅检查环境，尚未采样" if args.check_env else "未安装 rqdatac，未执行真实数据核验",
                  "next_step": "使用已有 RQData 授权环境，或安装 rqdatac 并按供应商指引配置 RQDATAC_CONF/SDK 许可"}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_CODES[report["status"]]
    stage = "load_halt_calendar"
    try:
        calendar = load_halt_calendar(args.halt_calendar)
        stage = "import_rqdatac"
        import rqdatac
        stage = "rqdatac_init"
        rqdatac.init(connect_timeout=5, timeout=API_TIMEOUT_SECONDS, auto_load_plugins=False, lazy=False)
        stage = "query_and_validate"
        report = run_probe(rqdatac, history_day=args.date, halt_calendar=calendar,
                           max_age=args.max_age_seconds)
    except Exception as exc:
        # SDK 异常统一在 CLI 边界显式失败。异常文本可能带授权 URI，不向报告泄露。
        report = {"status": "error", "stage": stage, "error_type": type(exc).__name__,
                  "reason": "RQData 请求或响应校验失败；未得出数据源通过/不通过结论",
                  "item_19_complete": False}
        if stage == "load_halt_calendar" or type(exc) is ValueError:
            report["reason"] = "输入配置或响应格式无效，请核对日期、停牌日历及供应商接口要求"
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return EXIT_CODES[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
