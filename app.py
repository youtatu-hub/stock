"""趋势分析实时买卖点工具 - API服务器。

启动：python app.py
API端口：8795  |  看板端口：同端口（/ → index.html）

API：
  GET /api/analyze?symbol=600000          全量分析
  GET /api/quote?symbol=600000            实时行情
  GET /api/fund?symbol=050025             场外基金净值与趋势
  GET /api/search?keyword=贵州             搜索股票
  GET /api/kline?symbol=600000&count=250  K线数据
"""
from __future__ import annotations

import json
import re
import hashlib
import sys
import os
import logging
import threading
import time
import concurrent.futures
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 确保项目根目录在path中
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from data.kline_fetcher import (
    fetch_kline, fetch_quote, fetch_fund_flow, search_stock, fetch_minute,
    fetch_realtime_flow, fetch_all_a_shares, fetch_index_kline, fetch_market_breadth,
    fetch_kline_extras, enrich_klines, quote_turnover_check,
    Kline, Quote, FundFlow, MinuteData, MinuteFlow, utc_now
)
from data.market_breadth import market_breadth_cache
from data.fund_flow_service import history_status, serialize_history, realtime_flow_service, analysis_snapshot
from data.fund_nav import fetch_fund_nav
from data.fund_terms import fetch_fund_terms
from data.etf_premium import fetch_etf_premium
from data.instruments import get_instrument, instrument_catalog
from data.ai_client import AIClient
from data.request_runtime import collect_stages, fetch_required, request_id
from analysis.signal_engine import run_analysis, SignalEngineResult
from analysis.signal_policy import apply_signal_optimization as _apply_signal_optimization
from analysis.chanlun_minute import analyze_chanlun_minute, signals_to_dict
from analysis.chanlun_daily import analyze_chanlun_daily, daily_result_to_dict
from analysis.fund_nav import analyze_fund_nav
from analysis.asset_analysis import analyze_listed_fund

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trend_app")

HOST = os.environ.get("STOCK_HOST", "127.0.0.1").strip()
if not HOST:
    raise RuntimeError("环境变量 STOCK_HOST 不能为空")

try:
    PORT = int(os.environ.get("STOCK_PORT", "8795"))
except ValueError as exc:
    raise RuntimeError("环境变量 STOCK_PORT 必须是整数") from exc
if not 1 <= PORT <= 65535:
    raise RuntimeError("环境变量 STOCK_PORT 必须在 1 到 65535 之间")

DASHBOARD_DIR = os.path.join(ROOT, "dashboard")
# count 参数安全解析上限，防止非法输入导致 500 或超大值放大网络请求
MAX_KLINE_COUNT = 10000
MAX_CHANLUN_COUNT = 10000


def _parse_count(params: dict, default: int = 250, max_count: int = MAX_KLINE_COUNT) -> int:
    """安全解析 count 查询参数：非法/超限时回退到默认值或钳制到上限。"""
    raw = params.get("count", [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, max_count)


# ---- 数据序列化 ----
def kline_to_dict(k: Kline) -> dict:
    return {
        "date": k.date, "open": k.open, "close": k.close,
        "high": k.high, "low": k.low, "volume": k.volume,
        "amount": k.amount, "pct": k.pct, "turnover": k.turnover,
        "fetched_at": k.fetched_at,
    }


def quote_to_dict(q: Quote) -> dict:
    return {
        "symbol": q.symbol, "name": q.name, "price": q.price, "pct": q.pct,
        "change": q.change, "high": q.high, "low": q.low, "open": q.open,
        "pre_close": q.pre_close, "volume": q.volume, "amount": q.amount,
        "turnover": q.turnover,
        "timestamp": q.timestamp, "fetched_at": q.fetched_at,
        "source": q.source, "volume_unit": q.volume_unit,
        "turnover_source": f"{q.source}提供的换手率，按数据源原值展示",
        "turnover_check": quote_turnover_check(q),
        "volume_source": q.source, "amount_source": q.source,
        "stale": q.stale, "refresh_error": q.refresh_error,
    }


def signal_to_dict(r: SignalEngineResult) -> dict:
    """将信号引擎结果序列化为JSON。"""
    data = {
        "action": r.action,
        "score": r.score,
        "confidence": r.confidence,
        "risk_level": r.risk_level,
        "signal_strength": r.signal_strength,
        "plain_summary": r.plain_summary,
        "trade_plan": r.trade_plan,
        "module_scores": r.module_scores,
        "buy_signals": r.buy_signals,
        "sell_signals": r.sell_signals,
        "risk_warnings": r.risk_warnings,
        "key_levels": r.key_levels,
        "description": r.description,
        "model": r.model,
        "asset_metrics": r.asset_metrics,
        "trend": None,
        "patterns": [],
        "volume_price": None,
        "breakouts": [],
        "canslim": None,
    }

    if r.trend:
        data["trend"] = {
            "direction": r.trend.direction,
            "strength": r.trend.strength,
            "stage": r.trend.stage,
            "ma_arrangement": r.trend.ma_arrangement,
            "ma_scores": r.trend.ma_scores,
            "trendline": r.trend.trendline,
            "signals": r.trend.signals,
        }

    for p in r.patterns:
        data["patterns"].append({
            "name": p.name,
            "direction": p.direction,
            "confidence": p.confidence,
            "status": p.status,
            "target_price": p.target_price,
            "key_levels": p.key_levels,
            "description": p.description,
        })

    if r.volume_price:
        data["volume_price"] = {
            "pattern": r.volume_price.pattern,
            "direction": r.volume_price.direction,
            "confidence": r.volume_price.confidence,
            "volume_ratio": r.volume_price.volume_ratio,
            "turnover": r.volume_price.turnover,
            "obv_trend": r.volume_price.obv_trend,
            "signals": r.volume_price.signals,
            "description": r.volume_price.description,
        }

    for b in r.breakouts:
        data["breakouts"].append({
            "system": b.system,
            "signal": b.signal,
            "direction": b.direction,
            "stop_reference_price": b.stop_reference_price,
            "breakout_price": b.breakout_price,
            "current_n": b.current_n,
            "stop_loss": b.stop_loss,
            "entry_price": b.entry_price,
            "position_units": b.position_units,
            "exit_price": b.exit_price,
            "channel_high": b.channel_high,
            "channel_low": b.channel_low,
            "next_add_price": b.next_add_price,
            "signals": b.signals,
            "description": b.description,
        })

    if r.canslim:
        data["canslim"] = {
            "c_score": r.canslim.c_score,
            "a_score": r.canslim.a_score,
            "n_score": r.canslim.n_score,
            "s_score": r.canslim.s_score,
            "l_score": r.canslim.l_score,
            "i_score": r.canslim.i_score,
            "m_score": r.canslim.m_score,
            "total": r.canslim.total,
            "grade": r.canslim.grade,
            "signals": r.canslim.signals,
            "cup_handle": r.canslim.cup_handle,
            "description": r.canslim.description,
        }

    return data


# ---- API处理 ----
def _kline_status(klines: list, period: str) -> dict:
    last = klines[-1] if klines else None
    return {"last_bar": last.date if last else None,
            "fetched_at": last.fetched_at if last else None, "period": period,
            "source": last.source if last else None, "stale": bool(last and last.stale),
            "refresh_error": last.refresh_error if last else ""}


def _analysis_inputs(klines, flows, index_klines, breadth, degraded):
    # 仅包含评分实际使用的数值；网络获取时间和独立报价不改变输入标识。
    inputs = {
        "klines": [(k.date, k.open, k.close, k.high, k.low, k.volume) for k in klines],
        "flows": [(f.date, f.main_net) for f in flows],
        "index": [(k.date, k.close) for k in index_klines or []],
        "breadth": {key: breadth.get(key) for key in ("total", "up", "down", "breadth_ratio")} if breadth else None,
    }
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:12]
    missing = []
    if klines[-1].stale:
        missing.append("kline")
    if not flows or "fund_flow" in degraded:
        missing.append("fund_flow")
    if len(index_klines or []) < 60 or "index" in degraded:
        missing.append("index")
    return {"id": fingerprint, "status": "provisional" if missing else "complete",
            "missing": missing, "kline_through": klines[-1].date,
            "kline_stale": klines[-1].stale, "kline_source": klines[-1].source,
            "flow_through": flows[-1].date if flows else None,
            "index_through": index_klines[-1].date if index_klines else None,
            "basis": "价格、成交量使用同一K线快照；相同输入得到相同评分，盘中K线或资金流变化会重算"}


def handle_analyze(params: dict) -> dict:
    started = time.monotonic()
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}

    period = params.get("period", ["day"])[0].strip()
    instrument = get_instrument(symbol)
    if instrument["model"] == "nav":
        return {"error": "场外基金请使用净值分析接口 /api/fund"}
    if instrument["model"] != "stock":
        return handle_asset_analyze(instrument, period)
    log.info(f"开始分析 {symbol} (period={period})")

    data, degraded, timings = collect_stages({
        "kline": lambda: fetch_kline(symbol, count=250, period=period, enrich=False),
        "quote": lambda: fetch_quote(symbol),
        "fund_flow": lambda: fetch_fund_flow(symbol, days=30),
        "index": lambda: fetch_index_kline("000001", count=60),
        "kline_extras": lambda: fetch_kline_extras(symbol, count=250, period=period),
    }, required=("kline",))
    klines = data.get("kline", [])
    if len(klines) < 30:
        return {"error": degraded.get("kline", f"K线数据不足: {len(klines)}条"),
                "degraded": degraded, "timings_ms": timings}
    if klines[-1].stale:
        degraded["kline"] = f"K线刷新未取得同等日期的新结果，使用最近成功快照（{klines[-1].fetched_at}）"
    else:
        klines = enrich_klines(klines, data.get("kline_extras", {}))
    quote = data.get("quote")
    if quote and quote.stale:
        degraded["quote"] = f"实时报价刷新失败，显示最近成功报价（{quote.fetched_at}）"
    flows = sorted((flow for flow in data.get("fund_flow", []) if flow.date <= klines[-1].date),
                   key=lambda flow: flow.date)
    flow_status = history_status(flows, klines, period)
    if flow_status["lagging"]:
        degraded["fund_flow"] = (f"日级资金流仅截至{flow_status['data_through']}，"
                                  f"K线截至{flow_status['reference_date']}")
    index_klines = data.get("index")
    breadth_started = time.monotonic()
    breadth_snapshot = market_breadth_cache.snapshot()
    breadth = breadth_snapshot.pop("data")
    timings["breadth_cache"] = round((time.monotonic() - breadth_started) * 1000, 1)
    if breadth_snapshot["stale"]:
        degraded["breadth"] = ("市场宽度使用最近成功缓存" if breadth else "市场宽度尚未就绪")
    scoring_breadth = breadth if breadth_snapshot["usable_for_scoring"] else None

    # 运行分析
    analysis_started = time.monotonic()
    result = run_analysis(klines, None if quote and quote.stale else quote, flows, index_klines, scoring_breadth)
    signal_data = signal_to_dict(result)

    # ---- 信号引擎优化：硬否决/软否决/分级体系/仓位管理/盈亏比 ----
    signal_data = _apply_signal_optimization(signal_data, klines, quote)
    signal_data["input_snapshot"] = _analysis_inputs(klines, flows, index_klines, scoring_breadth, degraded)

    # 构建大盘环境摘要
    market_env = ""
    if index_klines and len(index_klines) >= 20:
        idx_close = index_klines[-1].close
        idx_pct = index_klines[-1].pct
        idx_20d = (index_klines[-1].close - index_klines[-21].close) / index_klines[-21].close * 100 if len(index_klines) >= 21 else 0
        market_env = f"上证{idx_close:.1f}({idx_pct:+.2f}%) 20日{idx_20d:+.1f}%"
        if breadth:
            up_n = breadth.get("up", 0)
            down_n = breadth.get("down", 0)
            br = breadth.get("breadth_ratio", 0)
            market_env += f" | {up_n}涨{down_n}跌({br*100:.0f}%上涨)"

    timings["analysis"] = round((time.monotonic() - analysis_started) * 1000, 1)
    timings["total"] = round((time.monotonic() - started) * 1000, 1)
    log.info("analysis_done request_id=%s symbol=%s period=%s timings_ms=%s degraded=%s",
             request_id.get(), symbol, period, timings, degraded)
    return {
        "symbol": symbol,
        "instrument": instrument,
        "name": quote.name if quote else "",
        "quote": quote_to_dict(quote) if quote else None,
        "signal": signal_data,
        "klines": [kline_to_dict(k) for k in klines[-120:]],  # 返回最近120条K线
        "flows": serialize_history(flows),
        "flow_status": flow_status,
        "market_env": market_env,  # 大盘环境摘要
        "breadth": breadth,  # 市场宽度（涨跌家数）
        "breadth_status": breadth_snapshot,
        "kline_status": _kline_status(klines, period),
        "analyzed_at": utc_now(),
        "degraded": degraded,
        "timings_ms": timings,
    }


def handle_asset_analyze(instrument: dict, period: str) -> dict:
    """场内基金只读取适用输入，不触发个股评分、大盘仓位限制或固定目标价。"""
    symbol = instrument["code"]
    jobs = {
        "kline": lambda: fetch_kline(symbol, count=250, period=period, enrich=False),
        "quote": lambda: fetch_quote(symbol),
        "kline_extras": lambda: fetch_kline_extras(symbol, count=250, period=period),
    }
    if instrument["canslim"]:
        jobs["index"] = lambda: fetch_index_kline("000001", count=60)
        jobs["fund_flow"] = lambda: fetch_fund_flow(symbol, days=30)
    data, degraded, timings = collect_stages(jobs, required=("kline",))
    klines = data.get("kline", [])
    if len(klines) < 30:
        return {"error": degraded.get("kline", f"K线数据不足: {len(klines)}条"), "degraded": degraded}
    if klines[-1].stale:
        degraded["kline"] = "K线使用最近成功快照，本次刷新未取得同等日期的新结果"
    else:
        klines = enrich_klines(klines, data.get("kline_extras", {}))
    quote = data.get("quote")
    if quote and quote.stale:
        degraded["quote"] = "报价使用最近成功快照"
    flows = sorted((flow for flow in data.get("fund_flow", []) if flow.date <= klines[-1].date), key=lambda flow: flow.date)
    result = analyze_listed_fund(klines, instrument, data.get("index"), flows)
    signal = signal_to_dict(result)
    signal["input_snapshot"] = {
        "status": "provisional" if klines[-1].stale or instrument["canslim"] and (not flows or not data.get("index")) else "complete",
        "kline_through": klines[-1].date, "kline_stale": klines[-1].stale,
        "basis": "价格趋势与区间回撤按该品种K线计算。核心进攻ETF另提供CAN SLIM技术代理评分，不生成个股交易计划。",
    }
    return {"symbol": symbol, "instrument": instrument, "name": quote.name if quote else instrument["name"],
            "quote": quote_to_dict(quote) if quote else None, "signal": signal,
            "klines": [kline_to_dict(bar) for bar in klines[-120:]], "flows": serialize_history(flows),
            "flow_status": history_status(flows, klines, period, requested=instrument["canslim"]),
            "market_env": "", "breadth": None,
            "breadth_status": {}, "kline_status": _kline_status(klines, period),
            "analyzed_at": utc_now(), "degraded": degraded, "timings_ms": timings}


def handle_quote(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    # fetch_quote 自带预算和请求合并，不再排进分析/扫描共用的数据线程池。
    q = fetch_quote(symbol)
    return quote_to_dict(q) if q else {"error": "获取行情失败"}


def handle_fund(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    if not re.fullmatch(r"[0-9]{6}", symbol):
        return {"error": "基金代码必须为 6 位数字"}
    data = fetch_required("fund_nav", lambda: fetch_fund_nav(symbol))
    return {**data, **analyze_fund_nav(data["history"])}


def handle_fund_terms(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    if not re.fullmatch(r"[0-9]{6}", symbol):
        return {"error": "基金代码必须为 6 位数字", "invalid": True}
    instrument = get_instrument(symbol)
    codes = [symbol] + ([instrument["peer_code"]] if instrument.get("peer_code") else [])
    values, errors, _ = collect_stages(
        {code: lambda code=code: fetch_fund_terms(code) for code in codes}, required=tuple(codes))
    items = [{"instrument": get_instrument(code), "data": values.get(code), "error": errors.get(code)}
             for code in codes]
    result = {"symbol": symbol, "items": items, "status": "partial" if errors else "ok"}
    if not values:
        result.update(status="error", error="；".join(errors.values()))
    return result


def handle_etf_premium(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    if not re.fullmatch(r"[0-9]{6}", symbol) or get_instrument(symbol)["asset_type"] != "overseas_etf":
        return {"error": "仅支持已配置的海外ETF代码", "invalid": True}
    values, errors, _ = collect_stages({
        "iopv": lambda: fetch_etf_premium(symbol),
        "nav": lambda: fetch_fund_nav(symbol),
        "quote": lambda: fetch_quote(symbol),
    }, required=("iopv", "nav", "quote"))
    snapshot = values.get("iopv")
    nav = values.get("nav")
    quote = values.get("quote")
    disclosed = None
    if nav:
        latest = nav["history"][-1]
        verified_iopv = snapshot and snapshot.get("status") == "ok"
        price = snapshot["price"] if verified_iopv else quote.price if quote else snapshot.get("price") if snapshot else None
        price_time = snapshot["quote_time"] if verified_iopv else quote.timestamp if quote else snapshot.get("quote_time") if snapshot else None
        disclosed = {"value": latest["nav"], "date": latest["date"],
                     "price": price, "price_time": price_time,
                     "price_stale": False if verified_iopv else quote.stale if quote else not snapshot or not snapshot.get("snapshot_fresh"),
                     "premium_pct": round((price / latest["nav"] - 1) * 100, 3) if price else None,
                     "source": nav["source"], "source_url": nav["source_url"],
                     "fetched_at": nav["fetched_at"],
                     "basis": "本ETF已披露单位净值，非盘中估值；与当前交易价格可能不在同一时点。"}
    result = {"symbol": symbol, "iopv": snapshot, "disclosed_nav": disclosed,
              "status": "partial" if errors or not snapshot or snapshot.get("status") != "ok" else "ok", "errors": errors}
    if not values:
        result.update(status="error", error="；".join(errors.values()))
    return result


def handle_search(params: dict) -> dict:
    keyword = params.get("keyword", [""])[0].strip()
    if not keyword:
        return {"error": "缺少keyword参数"}
    # 无搜索结果本身是正常响应。
    results = fetch_required("search", lambda: {"results": search_stock(keyword)})["results"]
    return {"results": results}


def handle_kline(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    count = _parse_count(params, max_count=MAX_KLINE_COUNT)
    period = params.get("period", ["day"])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    basic = params.get("basic", ["0"])[0] == "1"
    if basic:
        klines = fetch_required("kline", lambda: fetch_kline(symbol, count=count, period=period, enrich=False))
        return {"klines": [kline_to_dict(k) for k in klines], "basic": True,
                "kline_status": _kline_status(klines, period)}
    data, degraded, timings = collect_stages({
        "kline": lambda: fetch_kline(symbol, count=count, period=period, enrich=False),
        "kline_extras": lambda: fetch_kline_extras(symbol, count=count, period=period),
    }, required=("kline",))
    if "kline" not in data:
        return {"error": degraded["kline"], "klines": [], "timings_ms": timings}
    klines = data["kline"]
    if not klines[-1].stale:
        klines = enrich_klines(klines, data.get("kline_extras", {}))
    return {"klines": [kline_to_dict(k) for k in klines], "degraded": degraded, "timings_ms": timings,
            "kline_status": _kline_status(klines, period)}


def handle_minute(params: dict) -> dict:
    """分时数据接口。"""
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    md = fetch_required("minute", lambda: fetch_minute(symbol))
    if not md:
        return {"error": "获取分时数据失败"}
    return {
        "symbol": symbol,
        "name": md.name,
        "pre_close": md.pre_close,
        "high": md.high,
        "low": md.low,
        "times": md.times,
        "prices": md.prices,
        "avg_prices": md.avg_prices,
        "volumes": md.volumes,
    }


def handle_chanlun_minute(params: dict) -> dict:
    """缠论分时分析接口。在分时数据上运行缠论分析，返回买卖点信号。"""
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    if not get_instrument(symbol)["chanlun"]:
        return {"error": "该资产类型未启用缠论模型"}
    md = fetch_required("minute", lambda: fetch_minute(symbol))
    if not md or not md.prices:
        return {"error": "获取分时数据失败"}
    # 运行缠论分析
    result = analyze_chanlun_minute(md.times, md.prices, md.volumes)
    return signals_to_dict(result)


def handle_chanlun_daily(params: dict) -> dict:
    """缠论日线/周线分析接口。在日K或周K线上运行完整缠论分析，返回买卖点、分型、笔、中枢及图表叠加数据。"""
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    if not get_instrument(symbol)["chanlun"]:
        return {"error": "该资产类型未启用缠论模型"}
    count = _parse_count(params, max_count=MAX_CHANLUN_COUNT)
    period = params.get("period", ["day"])[0].strip()
    klines = fetch_required("kline", lambda: fetch_kline(symbol, count=count, period=period, enrich=False))
    if not klines or len(klines) < 10:
        return {"error": f"K线数据不足（仅{len(klines) if klines else 0}根）"}
    dates = [k.date for k in klines]
    opens = [k.open for k in klines]
    closes = [k.close for k in klines]
    highs = [k.high for k in klines]
    lows = [k.low for k in klines]
    volumes = [k.volume for k in klines]
    result = analyze_chanlun_daily(dates, opens, closes, highs, lows, volumes)
    return {**daily_result_to_dict(result), "kline_status": _kline_status(klines, period)}


def handle_realtime_flow(params: dict) -> dict:
    """盘中实时分时资金流接口。返回当日1分钟级累计资金流。"""
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    return realtime_flow_service.snapshot(symbol, fetch_realtime_flow,
                                           force=params.get("refresh", ["0"])[0] == "1")


def handle_fund_flow(params: dict) -> dict:
    symbol = params.get("symbol", [""])[0].strip()
    if not symbol:
        return {"error": "缺少symbol参数"}
    values, errors, _ = collect_stages({
        "fund_flow": lambda: {"flows": fetch_fund_flow(symbol, days=30, force=True)},
    }, required=("fund_flow",), budget=8)
    flows = values.get("fund_flow", {}).get("flows") or []
    return {"symbol": symbol, "status": "error" if errors else "ok" if flows else "unavailable",
            "message": errors.get("fund_flow", ""), "flows": serialize_history(flows),
            "flow_status": history_status(flows), "checked_at": utc_now()}


# ---- 扫描功能 ----
_scan_state = {
    "status": "idle",        # idle | running | done | error
    "stage": "",             # 当前阶段描述
    "progress": 0,           # 0-100
    "total": 0,
    "scanned": 0,
    "found": 0,
    "results": [],
    "error": "",
    "start_time": 0,
    "elapsed": 0,
}
_scan_lock = threading.Lock()


def _scan_one_stock(symbol: str, period: str, index_klines, breadth) -> dict:
    """分析单只股票，返回简化结果。供扫描调用。"""
    try:
        klines = fetch_kline(symbol, count=250, period=period)
        if len(klines) < 30:
            return None
        quote = fetch_quote(symbol)
        if quote and quote.stale:
            return None
        flows = fetch_fund_flow(symbol, days=30)

        result = run_analysis(klines, quote, flows, index_klines, breadth)
        signal_data = signal_to_dict(result)

        # 信号引擎优化后处理
        signal_data = _apply_signal_optimization(signal_data, klines, quote)

        return {
            "symbol": symbol,
            "name": quote.name if quote else "",
            "price": quote.price if quote else 0,
            "action": signal_data.get("action", "观望"),
            "score": signal_data.get("score", 0),
            "confidence": signal_data.get("confidence", 0),
            "original_action": signal_data.get("original_action", ""),
            "veto_reason": signal_data.get("veto_reason", ""),
            "position_advice": signal_data.get("position_advice", ""),
            "risk_reward": signal_data.get("risk_reward", 0),
            "m_score": (signal_data.get("canslim") or {}).get("m_score", 50),
            "module_scores": signal_data.get("module_scores", {}),
            "risk_notes": signal_data.get("risk_notes", []),
        }
    except Exception as e:
        log.debug(f"扫描{symbol}({period})失败: {e}")
        return None


def _run_scan(max_stocks: int = 1000):
    """后台扫描全A股，日K+周K双周期买入筛选。"""
    global _scan_state
    try:
        with _scan_lock:
            _scan_state.update({
                "status": "running",
                "stage": "获取A股列表...",
                "progress": 0,
                "scanned": 0,
                "found": 0,
                "results": [],
                "error": "",
                "start_time": time.time(),
                "elapsed": 0,
            })

        # ---- 1. 获取全A股列表 ----
        all_stocks = fetch_all_a_shares()
        if not all_stocks:
            with _scan_lock:
                _scan_state["status"] = "error"
                _scan_state["error"] = "获取A股列表失败"
            return

        # ---- 2. 预过滤：排除ST/退市/停牌(价格=0) ----
        filtered = []
        for s in all_stocks:
            name = s.get("name", "")
            price = s.get("price", 0)
            # 排除ST、退市
            if "ST" in name or "退" in name:
                continue
            # 排除停牌/无报价的股票
            if not price or price <= 0:
                continue
            filtered.append(s)

        total_stage1 = len(filtered)
        log.info(f"扫描开始: 全A股{len(all_stocks)}只 → 过滤后{total_stage1}只（排除ST/退市）")

        with _scan_lock:
            _scan_state["total"] = total_stage1
            _scan_state["stage"] = f"日K扫描({total_stage1}只)..."

        # ---- 3. 预获取共享数据 ----
        index_klines = None
        try:
            index_klines = fetch_index_kline("000001", count=60)
        except Exception:
            pass

        breadth = None
        try:
            breadth = fetch_market_breadth()
        except Exception:
            pass

        # ---- 4. 并发日K扫描 ----
        daily_buy = []
        scanned_count = 0

        def scan_daily(stock):
            nonlocal scanned_count
            code = stock["code"]
            r = _scan_one_stock(code, "day", index_klines, breadth)
            with _scan_lock:
                scanned_count += 1
                _scan_state["scanned"] = scanned_count
                _scan_state["progress"] = round(scanned_count / max(total_stage1, 1) * 50, 1)
            if r and r["action"] in ("强烈买入", "买入", "谨慎买入"):
                r["daily_name"] = stock.get("name", "")
                r["daily_pct"] = stock.get("pct", 0)
                return r
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(scan_daily, s): s for s in filtered}
            for f in concurrent.futures.as_completed(futures):
                try:
                    r = f.result()
                    if r:
                        daily_buy.append(r)
                        with _scan_lock:
                            _scan_state["found"] = len(daily_buy)
                except Exception:
                    pass

        log.info(f"日K扫描完成: {total_stage1}只 → {len(daily_buy)}只有买入信号")

        # ---- 5. 对日K买入的股票，扫描周K ----
        with _scan_lock:
            _scan_state["total"] = len(daily_buy)
            _scan_state["scanned"] = 0
            _scan_state["stage"] = f"周K验证({len(daily_buy)}只)..."

        weekly_scanned = 0
        dual_buy = []

        def scan_weekly(stock):
            nonlocal weekly_scanned
            code = stock["symbol"]
            r = _scan_one_stock(code, "week", index_klines, breadth)
            with _scan_lock:
                weekly_scanned += 1
                _scan_state["scanned"] = weekly_scanned
                _scan_state["progress"] = 50 + round(weekly_scanned / max(len(daily_buy), 1) * 50, 1)
            if r and r["action"] in ("强烈买入", "买入", "谨慎买入"):
                return r
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = {executor.submit(scan_weekly, s): s for s in daily_buy}
            for f in concurrent.futures.as_completed(futures):
                try:
                    r = f.result()
                    if r:
                        # 找到对应的日K数据
                        daily = next((d for d in daily_buy if d["symbol"] == r["symbol"]), {})
                        dual_buy.append({
                            "symbol": r["symbol"],
                            "name": daily.get("daily_name", r.get("name", "")),
                            "price": daily.get("price", 0),
                            "daily_pct": daily.get("daily_pct", 0),
                            "daily_action": daily.get("action", ""),
                            "daily_score": daily.get("score", 0),
                            "daily_confidence": daily.get("confidence", 0),
                            "weekly_action": r["action"],
                            "weekly_score": r["score"],
                            "weekly_confidence": r["confidence"],
                            "combined_score": daily.get("score", 0) + r["score"],
                            "position_advice": daily.get("position_advice", ""),
                            "risk_reward": daily.get("risk_reward", 0),
                            "veto_reason": daily.get("veto_reason", ""),
                            "m_score": daily.get("m_score", 50),
                            "risk_notes": daily.get("risk_notes", []),
                        })
                        with _scan_lock:
                            _scan_state["found"] = len(dual_buy)
                except Exception:
                    pass

        # ---- 6. 排序取前20 ----
        dual_buy.sort(key=lambda x: x["combined_score"], reverse=True)
        results = dual_buy[:20]

        elapsed = round(time.time() - _scan_state["start_time"], 1)
        with _scan_lock:
            _scan_state.update({
                "status": "done",
                "stage": f"完成: {len(dual_buy)}只双周期买入，取前{len(results)}",
                "progress": 100,
                "results": results,
                "elapsed": elapsed,
            })
        log.info(f"扫描完成: {total_stage1}→{len(daily_buy)}→{len(dual_buy)}→TOP{len(results)}, 耗时{elapsed}s")

    except Exception as e:
        with _scan_lock:
            _scan_state["status"] = "error"
            _scan_state["error"] = str(e)
        log.error(f"扫描失败: {e}", exc_info=True)


def handle_scan(params: dict) -> dict:
    """扫描API：启动扫描或返回进度/结果。"""
    action = params.get("action", ["status"])[0]
    max_stocks = 0  # 0 = 全量扫描，不设上限

    if action == "start":
        with _scan_lock:
            if _scan_state["status"] == "running":
                return {"status": "running", "message": "扫描进行中，请等待..."}
            # 重置状态
            _scan_state.update({
                "status": "idle", "stage": "", "progress": 0,
                "total": 0, "scanned": 0, "found": 0,
                "results": [], "error": "", "elapsed": 0,
            })
        # 启动后台线程
        t = threading.Thread(target=_run_scan, args=(max_stocks,), daemon=True)
        t.start()
        return {"status": "started", "message": "扫描已启动"}

    # 默认返回当前状态
    with _scan_lock:
        state = dict(_scan_state)
    elapsed = state.get("elapsed", 0)
    if state["status"] == "running" and state.get("start_time"):
        elapsed = round(time.time() - state["start_time"], 1)
    return {
        "status": state["status"],
        "stage": state["stage"],
        "progress": state["progress"],
        "total": state["total"],
        "scanned": state["scanned"],
        "found": state["found"],
        "results": state["results"],
        "error": state.get("error", ""),
        "elapsed": elapsed,
    }


# ---- HTTP Handler ----
def handle_ai_analysis(body: dict) -> dict:
    symbol = body.get("symbol", "")
    period = body.get("period", "day")
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol) or period not in ("day", "week"):
        raise ValueError("分析代码必须为6位数字，周期必须为day或week")
    client = AIClient(body.get("model", ""))
    if not client.model.strip():
        raise ValueError("请先选择模型")
    instrument = get_instrument(symbol)
    fund_flow = None
    if instrument["fund_flow"]:
        reference = body.get("flow_snapshot")
        realtime = (realtime_flow_service.for_analysis(symbol, reference) if reference is not None else
                    analysis_snapshot(handle_realtime_flow({"symbol": [symbol]})))
        fund_flow = {"realtime": realtime}
    is_nav = instrument["model"] == "nav"
    data = (handle_fund({"symbol": [symbol]}) if is_nav else
            handle_analyze({"symbol": [symbol], "period": [period]}))
    if data.get("error"):
        raise OSError(data["error"])
    if is_nav:
        context = {**data, "history": data.get("history", [])[-30:], "instrument": instrument}
    else:
        context = {key: data.get(key) for key in (
            "symbol", "instrument", "quote", "signal", "analyzed_at", "degraded", "kline_status")}
        if fund_flow is not None:
            fund_flow["daily_history"] = data.get("flow_status") or history_status([], requested=False)
            context["fund_flow"] = fund_flow
        context["recent_klines"] = data.get("klines", [])[-20:]
        if instrument["asset_type"] == "overseas_etf":
            premium = handle_etf_premium({"symbol": [symbol]})
            snapshot = premium.get("iopv")
            # 无独立估值时间的原始参考数字不进入模型输入。
            context["premium_reference"] = {**premium, "iopv":
                {key: value for key, value in snapshot.items() if key != "reported_reference"} if snapshot else None}
    context["period"] = period
    return {"symbol": symbol, "period": period, "model": client.model,
            "text": client.analyze(context), "analyzed_at": utc_now(),
            "fund_flow": fund_flow,
            "data_analyzed_at": data.get("analyzed_at") or data.get("fetched_at")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默日志，避免刷屏

    def _send_no_cache_headers(self):
        """防止浏览器缓存旧版页面"""
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self._send_no_cache_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: bytes, content_type: str = "text/html", immutable: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        if immutable:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self._send_no_cache_headers()
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/ai/models", "/api/ai/analyze"):
            self._json({"error": "未知API"}, 404)
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._json({"error": "AI 配置仅接受同源请求"}, 403)
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求必须使用 application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("请求体大小无效")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求体必须是JSON对象")
            allowed = set() if path.endswith("/models") else {"symbol", "period", "model", "flow_snapshot"}
            if set(body) - allowed:
                raise ValueError("请求包含不支持的字段；CPA连接由后端配置")
            result = AIClient().models() if path.endswith("/models") else handle_ai_analysis(body)
            self._json(result)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.info("AI 请求的客户端已断开")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except OSError as exc:
            self._json({"error": str(exc)}, 503)
        except Exception:
            log.exception("AI接口内部错误")
            self._json({"error": "AI接口内部错误，请查看服务日志"}, 500)
        finally:
            self.connection.settimeout(None)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # API路由
        if path.startswith("/api/"):
            started = time.monotonic()
            token = request_id.set(uuid.uuid4().hex[:12])
            try:
                if path == "/api/health":
                    self._json({"status": "ok", "time": time.strftime("%H:%M:%S"),
                                "flow_api_version": 7, "flow_sources": ["腾讯", "东方财富"],
                                "ai_api_version": 3, "premium_api_version": 4,
                                "breadth_sources": ["新浪", "东方财富"]})
                elif path == "/api/analyze":
                    self._json(handle_analyze(params))
                elif path == "/api/market_breadth":
                    self._json(market_breadth_cache.snapshot())
                elif path == "/api/quote":
                    self._json(handle_quote(params))
                elif path == "/api/fund":
                    result = handle_fund(params)
                    self._json(result, 400 if "error" in result else 200)
                elif path == "/api/instruments":
                    self._json(instrument_catalog())
                elif path in ("/api/fund-terms", "/api/etf-premium"):
                    result = (handle_fund_terms if path == "/api/fund-terms" else handle_etf_premium)(params)
                    self._json(result, 400 if result.get("invalid") else 503 if "error" in result else 200)
                elif path == "/api/search":
                    self._json(handle_search(params))
                elif path == "/api/kline":
                    self._json(handle_kline(params))
                elif path == "/api/minute":
                    self._json(handle_minute(params))
                elif path == "/api/chanlun_minute":
                    self._json(handle_chanlun_minute(params))
                elif path == "/api/chanlun_daily":
                    self._json(handle_chanlun_daily(params))
                elif path == "/api/realtime_flow":
                    self._json(handle_realtime_flow(params))
                elif path == "/api/fund_flow":
                    self._json(handle_fund_flow(params))
                elif path == "/api/scan":
                    self._json(handle_scan(params))
                else:
                    self._json({"error": "未知API"}, 404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.info("客户端已取消请求: %s", path)
            except OSError as e:
                log.warning("API数据获取失败: %s", e)
                self._json({"error": str(e)}, 503)
            except Exception as e:
                log.error(f"API错误: {e}", exc_info=True)
                self._json({"error": str(e)}, 500)
            finally:
                log.info("api_done request_id=%s path=%s symbol=%s period=%s elapsed_ms=%.1f",
                         request_id.get(), path, params.get("symbol", [""])[0],
                         params.get("period", ["day"])[0], (time.monotonic() - started) * 1000)
                request_id.reset(token)
            return

        # 静态文件（看板）
        if path == "/" or path == "/index.html":
            filepath = os.path.join(DASHBOARD_DIR, "index.html")
        else:
            # 安全处理静态文件路径
            safe_path = path.lstrip("/")
            filepath = os.path.normpath(os.path.join(DASHBOARD_DIR, safe_path))
            if os.path.commonpath([filepath, DASHBOARD_DIR]) != DASHBOARD_DIR:
                self._json({"error": "禁止访问"}, 403)
                return

        if os.path.isfile(filepath):
            ext = os.path.splitext(filepath)[1].lower()
            ct = {
                ".html": "text/html", ".js": "application/javascript",
                ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg",
                ".svg": "image/svg+xml", ".ico": "image/x-icon",
            }.get(ext, "application/octet-stream")
            with open(filepath, "rb") as f:
                self._html(f.read(), ct, immutable=path == "/vendor/echarts-5.5.0.min.js")
        else:
            self._json({"error": "文件不存在"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    # ThreadingHTTPServer: 多线程处理，浏览器并发请求不会卡死
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    market_breadth_cache.start()
    log.info(f"趋势分析实时买卖点工具启动 → http://{HOST}:{PORT}")
    log.info(f"API: /api/analyze?symbol=600000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("服务停止")
    finally:
        market_breadth_cache.stop()
        # 无论正常还是异常退出，都释放监听端口与资源
        server.shutdown()
        server.server_close()
        log.info("服务资源已释放")


if __name__ == "__main__":
    main()
