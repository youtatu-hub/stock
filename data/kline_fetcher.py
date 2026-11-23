"""数据层：K线、实时报价及分时资金流优先腾讯，东财补充历史资金流和搜索。

参考《A股资金流向监控工具-个股增强版》的host池轮换、fltt=2、session复用设计。

数据源策略：
- K线价格/成交量：腾讯API前复权（价格准确，和行情一致）
- K线成交额/换手率：东财K线API（有值，按日期匹配补充）
- 实时行情：腾讯报价优先，东方财富独立回退；失败保留有时限的成功快照
- 分时资金流：腾讯hsfundtab优先，东方财富fflow/kline独立回退
- 日级资金流：东财fflow/daykline（push2his优先，push2test备选）
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import socket
import logging
import threading
import concurrent.futures
import contextvars
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# 便携依赖目录：无内置 python/ 时，从发行包 libs/ 加载第三方依赖
_LIBS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs")
if os.path.isdir(_LIBS_DIR):
    sys.path.insert(0, _LIBS_DIR)

import requests
from urllib3.util import Timeout

from data.request_runtime import (
    HTTP_ATTEMPT_SECONDS, collect_stages, data_stage, deadline_scope, remaining_seconds, request_id,
)
from data.market_time import flow_session_status
from data.instruments import get_instrument

log = logging.getLogger("trend_data")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quote_timestamp(value) -> str:
    try:
        stamp = float(value)
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds") if stamp > 0 else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# 保留原版可用的东财兼容解析；HTTPS Host/SNI 及证书校验仍使用原域名。
_PUSH2DELAY_IP = "117.184.45.167"
_original_getaddrinfo = socket.getaddrinfo
_dns_redirected = False
_dns_lock = threading.Lock()


def _patch_dns():
    global _dns_redirected
    mode = os.environ.get("STOCK_EASTMONEY_DNS_MODE", "legacy").strip().lower()
    if mode not in ("legacy", "system"):
        raise ValueError("STOCK_EASTMONEY_DNS_MODE 只能是 legacy 或 system")
    if mode == "system":
        return
    with _dns_lock:
        if _dns_redirected:
            return

        def resolve(host, port, family=0, type=0, proto=0, flags=0):
            use_legacy = os.environ.get(
                "STOCK_EASTMONEY_DNS_MODE", "legacy"
            ).strip().lower() == "legacy"
            if use_legacy and isinstance(host, str) and (
                    host in ("push2.eastmoney.com", "push2his.eastmoney.com")
                    or host.endswith(".push2his.eastmoney.com")):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUSH2DELAY_IP, port or 443))]
            return _original_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = resolve
        _dns_redirected = True


# ---- host池 ----
QUOTE_HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2test.eastmoney.com",
    "https://push2.eastmoney.com",
]
HIS_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://push2test.eastmoney.com",
    "https://82.push2his.eastmoney.com",
    "https://90.push2his.eastmoney.com",
    "https://push2delay.eastmoney.com",
]
SEARCH_HOST = "https://searchapi.eastmoney.com"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/"
TENCENT_FLOW = "https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab"
FLOW_PRIMARY_BUDGET_SECONDS = 3.0
QUOTE_SOURCE_BUDGET_SECONDS = 3.0
QUOTE_STALE_TTL_SECONDS = 15 * 60
QUOTE_RETRY_SECONDS = 10
QUOTE_MAX_LAG_SECONDS = 120

UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101",
]

# Session 的 headers/cookies 不跨线程共享。
_sessions = threading.local()
_cache_lock = threading.Lock()

_cache: Dict[str, Tuple[Any, float]] = {}
_CACHE_TTL = 15


def _get_session() -> requests.Session:
    session = getattr(_sessions, "session", None)
    if session is None:
        _patch_dns()
        session = requests.Session()
        # 恢复旧版直连，避免宿主机或系统代理悄悄改变行情请求线路。
        session.trust_env = False
        proxy = os.environ.get("STOCK_DATA_PROXY", "").strip()
        if proxy and urlsplit(proxy).scheme not in ("http", "https"):
            raise ValueError("STOCK_DATA_PROXY 必须是 http:// 或 https:// 代理地址")
        session.proxies = {"http": proxy or None, "https": proxy or None}
        session.headers.update({
            "User-Agent": UA_POOL[0],
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://quote.eastmoney.com/",
            "Connection": "keep-alive",
        })
        _sessions.session = session
    return session


def _rotate_ua() -> None:
    _sessions.ua_idx = (getattr(_sessions, "ua_idx", 0) + 1) % len(UA_POOL)
    _get_session().headers["User-Agent"] = UA_POOL[_sessions.ua_idx]


def _http_get(url: str, **kwargs):
    budget = remaining_seconds(HTTP_ATTEMPT_SECONDS)
    started = time.monotonic()
    try:
        response = _get_session().get(
            url, timeout=Timeout(total=budget, connect=budget, read=budget), **kwargs)
        response.raise_for_status()
        return response
    finally:
        log.info("http_fetch request_id=%s url=%s elapsed_ms=%.1f",
                 request_id.get(), url, (time.monotonic() - started) * 1000)


def _cached(key: str) -> Optional[Any]:
    return _cache_get(key, _CACHE_TTL)


def _set_cache(key: str, val: Any) -> None:
    _cache_set(key, val)


def _cache_get(key: str, ttl: float) -> Optional[Any]:
    """带自定义TTL的缓存读取，None表示未命中。"""
    with _cache_lock:
        e = _cache.get(key)
    if e and time.monotonic() - e[1] < ttl:
        log.info("data_cache request_id=%s key=%s status=hit", request_id.get(), key)
        return e[0]
    return None


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (val, time.monotonic())


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _source_error(exc: Exception) -> str:
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "连接或读取超时"
    if isinstance(exc, requests.HTTPError):
        return f"HTTP {exc.response.status_code}" if exc.response is not None else "HTTP请求失败"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "代理连接失败"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS连接失败"
    if isinstance(exc, requests.ConnectionError):
        text = str(exc)
        if "NameResolutionError" in text or "Failed to resolve" in text:
            return "DNS解析失败"
        if "Connection refused" in text:
            return "连接被拒绝"
        return "连接断开或无法建立连接"
    return "响应格式无效"


def _get_json_eastmoney(path: str, params: dict, host_pool: List[str], *, strict=False) -> Optional[dict]:
    """东财API请求，带host池轮换。path如/api/qt/stock/get"""
    current_url = host_pool[0] + path
    empty_response = None
    errors = []
    for attempt in range(len(host_pool)):
        try:
            r = _http_get(current_url, params=params)
            if r.status_code == 200:
                data = r.json()
                if data and data.get("data") is not None:
                    # 检查klines是否为空列表（push2delay对kline接口返回空klines）
                    d = data["data"]
                    if isinstance(d, dict) and "klines" in d and not d["klines"]:
                        empty_response = data
                        log.debug(f"host返回空klines，尝试下一个: {current_url}")
                    else:
                        return data
                else:
                    errors.append(f"{urlsplit(current_url).hostname}：未返回数据")
        except (requests.RequestException, ValueError, TimeoutError) as e:
            errors.append(f"{urlsplit(current_url).hostname}：{_source_error(e)}")
            log.warning(f"请求失败 try={attempt+1} {current_url}: {e}")
        _rotate_ua()
        current_url = host_pool[(attempt + 1) % len(host_pool)] + path
    log.warning("数据源均未返回有效结果: %s", path)
    if strict and empty_response is None:
        raise OSError("；".join(errors) or "数据源未返回有效响应")
    return empty_response


_source_executor = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="flow-source")
_source_slots = threading.BoundedSemaphore(24)


def _get_flow_json(path: str, params: dict, hosts: List[str]) -> dict:
    """并行读取资金流节点，在请求预算内选择日期最新的结果。"""
    budget = remaining_seconds(HTTP_ATTEMPT_SECONDS)
    # 预留结果解析时间，避免慢节点耗尽父请求预算后丢弃已取得的数据。
    deadline = time.monotonic() + budget - min(0.25, budget * 0.1)
    futures, candidates, errors = {}, [], []
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    def fetch(host):
        with deadline_scope(deadline):
            response = _http_get(host + path, params=params).json()
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise ValueError("未返回资金流数据")
        response["fetched_at"] = utc_now()
        return response

    try:
        for host in hosts:
            if not _source_slots.acquire(blocking=False):
                errors.append(f"{urlsplit(host).hostname}：请求队列已满")
                continue
            context = contextvars.copy_context()
            try:
                future = _source_executor.submit(context.run, fetch, host)
            except Exception:
                _source_slots.release()
                raise
            future.add_done_callback(lambda _: _source_slots.release())
            futures[future] = host
        try:
            for future in concurrent.futures.as_completed(futures, timeout=max(0, deadline - time.monotonic())):
                try:
                    response = future.result()
                    rows = response["data"].get("klines") or []
                    latest = max((row.split(",", 1)[0] for row in rows), default="")
                    candidates.append((latest, response))
                    if latest[:10] >= today:
                        return response
                except (requests.RequestException, OSError, ValueError) as exc:
                    errors.append(f"{urlsplit(futures[future]).hostname}：{_source_error(exc)}")
        except TimeoutError:
            errors.append("资金流节点查询超过时间预算")
    finally:
        for future in futures:
            future.cancel()
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    raise OSError("；".join(errors) or "资金流数据源不可用")


# ---- 代码转换 ----
def symbol_to_secid(symbol: str) -> str:
    symbol = str(symbol).strip().zfill(6)
    if symbol.startswith("920"):
        return f"0.{symbol}"
    if symbol.startswith(("5", "6", "7", "9")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def symbol_to_tencent(symbol: str) -> str:
    """转腾讯代码。6/5开头=沪市(sh)，其余=深市(sz)。"""
    symbol = symbol.strip()
    if symbol.startswith(("6", "5")):
        return f"sh{symbol}"
    return f"sz{symbol}"


# ---- K线 ----
@dataclass
class Kline:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float  # 成交量(手)
    amount: float = 0.0  # 成交额(元)
    pct: float = 0.0  # 涨跌幅(%)
    turnover: float = 0.0  # 换手率(%)
    fetched_at: str = ""
    source: str = ""
    stale: bool = False
    refresh_error: str = ""


def exclude_preopen_bar(klines: List[Kline]) -> List[Kline]:
    """盘前不采用当日日K；开盘后仍排除没有成交的当日占位行。"""
    session = flow_session_status()
    if session["session_state"] == "preopen":
        return [k for k in klines if k.date < session["market_date"]]
    return [k for k in klines if k.date != session["market_date"] or k.volume > 0]


def fetch_kline(symbol: str, count: int = 250, period: str = "day", adjust: str = "qfq",
                enrich: bool = True) -> List[Kline]:
    """首屏和缠论只读取价格；完整分析可独立并行获取补充字段。"""
    klines = _fetch_price_klines(symbol, count, period, adjust)
    if period == "day":
        klines = exclude_preopen_bar(klines)
    if not enrich or not klines:
        return klines
    return enrich_klines(klines, fetch_kline_extras(symbol, count, period))


def enrich_klines(klines: List[Kline], extras: dict) -> List[Kline]:
    # 只复制匹配项，避免修改正在被首屏/缠论读取的价格缓存。
    return [replace(k, amount=extras[k.date][0], turnover=extras[k.date][1])
            if k.date in extras else k for k in klines]


KLINE_SNAPSHOT_TTL_SECONDS = 15 * 60
KLINE_SOURCE_BUDGET_SECONDS = 2.5


def _valid_price_klines(klines: List[Kline], period: str) -> List[Kline]:
    """先校验、排除占位行，再判断来源是否覆盖最新交易日。"""
    valid_klines = []
    for k in klines:
        if k.close > 0 and k.high > 0 and k.low > 0 and k.open > 0:
            if k.high >= k.low and k.high >= k.close and k.high >= k.open:
                if k.low <= k.close and k.low <= k.open:
                    if k.close < 10000:  # 合理价格上限
                        valid_klines.append(k)
                        continue
        log.warning("异常K线数据被过滤 date=%s O=%s H=%s L=%s C=%s", k.date, k.open, k.high, k.low, k.close)
    valid_klines.sort(key=lambda k: k.date)
    return exclude_preopen_bar(valid_klines) if period == "day" else valid_klines


@data_stage
def _fetch_price_klines(symbol: str, count: int, period: str, adjust: str) -> List[Kline]:
    cache_key = f"kline_{symbol}_{count}_{period}_{adjust}"
    cached = _cached(cache_key)
    if cached:
        return cached
    previous = _cache_get(cache_key, KLINE_SNAPSHOT_TTL_SECONDS) or []
    previous = _valid_price_klines(previous, period)
    latest_known = previous or _valid_price_klines(_cache_get(cache_key, float("inf")) or [], period)
    failures = []
    # 东财含盘中日K；新浪可能仅到上一交易日，不能直接覆盖已取得的新结果。
    sources = (
        ("腾讯", lambda: _fetch_kline_tencent(symbol, count, period, adjust)),
        ("东方财富", lambda: _fetch_kline_eastmoney(symbol, count, period)),
        ("新浪", lambda: _fetch_kline_sina(symbol, count, period)),
    )
    for source, fetcher in sources:
        try:
            with deadline_scope(time.monotonic() + KLINE_SOURCE_BUDGET_SECONDS):
                klines = _valid_price_klines(fetcher(), period)
        except (requests.RequestException, OSError, ValueError) as exc:
            failures.append(f"{source}：{exc}")
            continue
        if not klines:
            failures.append(f"{source}未返回有效K线")
            continue
        if latest_known and klines[-1].date < latest_known[-1].date:
            failures.append(f"{source}仅截至{klines[-1].date}，早于已取得的{latest_known[-1].date}")
            continue
        fetched_at = utc_now()
        klines = [replace(k, fetched_at=fetched_at, source=source) for k in klines]
        _set_cache(cache_key, klines)
        return klines

    reason = "；".join(failures)
    log.warning("kline_refresh_failed symbol=%s reason=%s", symbol, reason)
    if previous:
        # 不刷新成功快照的时间、不拼接两家历史，也不伪造今天的K线。
        return [replace(k, stale=True, refresh_error=reason) for k in previous]
    raise OSError(f"K线暂不可用：{reason}")


def _fetch_kline_tencent(symbol: str, count: int, period: str, adjust: str) -> List[Kline]:
    """腾讯API前复权K线。价格准确，但无成交额。可能被WAF拦截。"""
    tc_symbol = symbol_to_tencent(symbol)
    fq = adjust if adjust else ""
    params = {"param": f"{tc_symbol},{period},,,{count},{fq}"}

    try:
        r = _http_get(TENCENT_KLINE, params=params)
        # WAF拦截检测：先尝试JSON解析，只有解析失败或返回HTML验证页才判定拦截
        # 腾讯API返回Content-Type=text/html但内容可能是有效JSON，不能仅靠Content-Type判断
        ct = r.headers.get("Content-Type", "")
        text = r.text.strip()
        # 1. 如果文本以<!DOCTYPE开头，必定是HTML验证页
        if text.startswith("<!DOCTYPE") or text.startswith("<html"):
            log.warning(f"腾讯K线被WAF拦截(HTML页面) {symbol}")
            return []
        # 2. 尝试JSON解析——即使Content-Type是text/html，数据也可能是有效JSON
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            # JSON解析失败，检查是否是HTML验证页
            if "text/html" in ct or "<" in text[:50]:
                log.warning(f"腾讯K线被WAF拦截(JSON解析失败) {symbol}")
            else:
                log.error(f"腾讯K线JSON解析失败 {symbol}: {text[:100]}")
            return []

        # 3. JSON解析成功但code!=0，可能是API错误
        if data.get("code") != 0:
            log.warning(f"腾讯K线API返回code={data.get('code')} {symbol}")
            return []

        stock_data = data.get("data", {}).get(tc_symbol, {})
        key = f"{fq}day" if period == "day" else f"{fq}week" if period == "week" else f"{fq}month"
        rows = stock_data.get(key, stock_data.get("day", stock_data.get("week", [])))

        klines: List[Kline] = []
        for row in rows:
            if len(row) >= 6:
                vol_raw = row[5]
                vol = float(vol_raw) if isinstance(vol_raw, (str, int, float)) else 0.0
                # 腾讯股票和ETF的K线成交量均以手计。
                k = Kline(
                    date=str(row[0]),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=vol,
                )
                if klines:
                    k.pct = round((k.close - klines[-1].close) / klines[-1].close * 100, 2)
                klines.append(k)
        return klines
    except (requests.RequestException, ValueError) as e:
        log.error(f"腾讯K线失败 {symbol}: {e}")
        return []


SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

def _sina_symbol(symbol: str) -> str:
    """转新浪代码前缀。6/5=sh, 920=bj, 其余=sz。"""
    symbol = symbol.strip()
    if symbol.startswith(("6", "5")):
        return f"sh{symbol}"
    if symbol.startswith("920"):
        return f"bj{symbol}"
    return f"sz{symbol}"


def _fetch_kline_sina(symbol: str, count: int, period: str) -> List[Kline]:
    """新浪API K线。不复权但价格准确，有成交量(股)无成交额。"""
    sina_sym = _sina_symbol(symbol)
    scale_map = {"day": "240", "week": "1200", "month": "7200"}
    scale = scale_map.get(period, "240")
    params = {"symbol": sina_sym, "scale": scale, "ma": "no", "datalen": str(count)}

    try:
        r = _http_get(SINA_KLINE, params=params)
        if r.status_code != 200:
            return []
        items = r.json()
        if not isinstance(items, list) or not items:
            return []

        klines: List[Kline] = []
        for item in items:
            d = item.get("day", "")
            o = _to_float(item.get("open"))
            c = _to_float(item.get("close"))
            h = _to_float(item.get("high"))
            lo = _to_float(item.get("low"))
            v = _to_float(item.get("volume"))
            if None in (o, c, h, lo) or not d:
                continue
            # 新浪股票单位为股、基金为份；统一换算为手，与腾讯/东财一致。
            vol = v / 100.0 if v else 0.0
            k = Kline(date=d, open=o, close=c, high=h, low=lo, volume=vol)
            if klines:
                k.pct = round((k.close - klines[-1].close) / klines[-1].close * 100, 2)
            klines.append(k)
        # 新浪返回的数据已是正序(旧→新)，无需反转
        return klines
    except (requests.RequestException, ValueError) as e:
        log.error(f"新浪K线失败 {symbol}: {e}")
        return []


EM_KLINE_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://82.push2his.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2test.eastmoney.com",
]

def _fetch_kline_eastmoney(symbol: str, count: int, period: str) -> List[Kline]:
    """东财K线(push2test优先)。有成交额/换手率，但价格可能不准(仅做最后fallback)。"""
    secid = symbol_to_secid(symbol)
    klt = "101" if period == "day" else "102" if period == "week" else "103"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": "1",
        "lmt": str(count),
        "end": "20500101",
    }

    data = _get_json_eastmoney("/api/qt/stock/kline/get", params, EM_KLINE_HOSTS)
    if not data or not data.get("data") or not data["data"].get("klines"):
        return []

    klines: List[Kline] = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        if len(parts) >= 7:
            k = Kline(
                date=parts[0],
                open=float(parts[1]),
                close=float(parts[2]),
                high=float(parts[3]),
                low=float(parts[4]),
                volume=float(parts[5]),
                amount=_to_float(parts[6]) or 0.0,
            )
            if len(parts) >= 11:
                k.turnover = _to_float(parts[10]) or 0.0
            if klines:
                k.pct = round((k.close - klines[-1].close) / klines[-1].close * 100, 2)
            klines.append(k)
    return klines


@data_stage
def fetch_kline_extras(symbol: str, count: int = 250, period: str = "day") -> dict:
    """独立获取对应周期的成交额和换手率，不阻塞基础K线。"""
    cache_key = f"kline_extras_{symbol}_{count}_{period}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached
    secid = symbol_to_secid(symbol)
    # 多请求一些数据以确保日期覆盖（东财可能缺少部分历史数据）
    request_count = min(count + 60, 500)
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101" if period == "day" else "102" if period == "week" else "103",
        "fqt": "0",  # 不复权（只取成交额和换手率，价格不用）
        "lmt": str(request_count),
        "end": "20500101",
    }

    data = _get_json_eastmoney("/api/qt/stock/kline/get", params, EM_KLINE_HOSTS)
    if not data or not data.get("data") or not data["data"].get("klines"):
        return {}

    # 构建日期→(amount, turnover)映射
    em_map: Dict[str, Tuple[float, float]] = {}
    for line in data["data"]["klines"]:
        parts = line.split(",")
        if len(parts) >= 11:
            date = parts[0]
            amount = _to_float(parts[6]) or 0.0
            turnover = _to_float(parts[10]) or 0.0
            em_map[date] = (amount, turnover)

    if em_map:
        _set_cache(cache_key, em_map)
    return em_map


# ---- 实时行情 ----
@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    pct: float
    change: float
    high: float
    low: float
    open: float
    pre_close: float
    volume: float  # 成交量(手)
    amount: float  # 成交额(元)
    turnover: float  # 换手率(%)
    timestamp: str = ""
    fetched_at: str = ""
    source: str = "东方财富"
    volume_unit: str = "手"
    stale: bool = False
    refresh_error: str = ""
    float_shares: Optional[float] = None


def quote_turnover_check(quote: Quote) -> dict:
    shares = quote.float_shares
    calculated = quote.volume * 100 / shares * 100 if shares and shares > 0 else None
    difference = abs(quote.turnover - calculated) if calculated is not None else None
    return {"float_shares": shares, "calculated_pct": round(calculated, 2) if calculated is not None else None,
            "status": "unavailable" if calculated is None else
            "consistent" if difference <= max(0.05, calculated * 0.02) else "mismatch",
            "formula": "成交量(手) × 100 ÷ 同源流通份额(股/份) × 100%",
            "source": quote.source}


def _validate_quote(quote: Quote) -> None:
    if not all(math.isfinite(value) and value >= 0
               for value in (quote.volume, quote.amount, quote.turnover)):
        raise ValueError("报价成交量、成交额或换手率无效")
    if quote.volume > 0 and quote.amount > 0 and quote.low > 0 and quote.high > 0:
        average = quote.amount / (quote.volume * 100)
        # 容忍源端金额舍入；十倍单位误差或跨品种拼接会落到区间之外。
        if not quote.low * 0.98 <= average <= quote.high * 1.02:
            raise ValueError("报价成交量与成交额不一致，成交均价超出当日高低价")


def quote_delay_reason(quote: Quote, now=None) -> str:
    if not quote.timestamp:
        return ""
    now = now or datetime.now(timezone(timedelta(hours=8)))
    clock = now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:32" <= clock <= "11:30" or "13:02" <= clock <= "15:00"):
        return ""
    age = (now - datetime.fromisoformat(quote.timestamp)).total_seconds()
    return f"来源行情时间落后{int(age)}秒" if age > QUOTE_MAX_LAG_SECONDS else ""


@data_stage
def fetch_quote(symbol: str) -> Optional[Quote]:
    """独立报价源依次限时尝试；失败不会删除或刷新成功快照的时间。"""
    cache_key = f"quote_{symbol}"
    cached = _cache_get(cache_key, _RT_CACHE_TTL)
    if cached:
        return cached
    previous = _cache_get(cache_key, QUOTE_STALE_TTL_SECONDS)
    retry_key = f"quote_retry_{symbol}"
    last_error = _cache_get(retry_key, QUOTE_RETRY_SECONDS)
    if not last_error:
        failures = []
        for source, fetcher in (("腾讯", _fetch_quote_tencent), ("东方财富", _fetch_quote_eastmoney)):
            try:
                with deadline_scope(time.monotonic() + QUOTE_SOURCE_BUDGET_SECONDS):
                    quote = fetcher(symbol)
                if quote and math.isfinite(quote.price) and quote.price > 0:
                    _validate_quote(quote)
                    delay = quote_delay_reason(quote)
                    if previous and quote.timestamp and previous.timestamp and quote.timestamp < previous.timestamp:
                        failures.append(f"{source}返回的行情时间倒退")
                        continue
                    if delay:
                        failures.append(f"{source}{delay}")
                        if previous is None or quote.timestamp > previous.timestamp:
                            previous = quote
                        continue
                    if (not quote.name or quote.name == symbol) and previous:
                        quote = replace(quote, name=previous.name)
                    _cache_set(cache_key, quote)
                    return quote
                failures.append(f"{source}未返回有效报价")
            except (requests.RequestException, OSError, ValueError) as exc:
                failures.append(f"{source}请求失败或超时")
                log.warning("quote_source_failed symbol=%s source=%s error=%s", symbol, source, exc)
        last_error = "；".join(failures)
        _cache_set(retry_key, last_error)
    if previous:
        return replace(previous, stale=True, refresh_error=last_error)
    raise OSError(f"报价暂不可用：{last_error}；稍后重试")


def _fetch_quote_tencent(symbol: str) -> Optional[Quote]:
    code = f"bj{symbol}" if symbol.startswith("920") else symbol_to_tencent(symbol)
    response = _http_get(TENCENT_QUOTE, params={"q": code})
    text = response.content.decode("gb18030")
    if '="' not in text:
        return None
    fields = text.split('="', 1)[1].split('"', 1)[0].split("~")
    if len(fields) < 39 or fields[2] != symbol:
        return None

    def number(index):
        return _to_float(fields[index]) or 0.0

    timestamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
        tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat(timespec="seconds")
    trade = fields[35].split("/")
    amount = (_to_float(trade[2]) if len(trade) >= 3 else None)
    return Quote(
        symbol=symbol, name=fields[1].strip(), price=number(3), pct=number(32),
        change=number(31), high=number(33), low=number(34), open=number(5),
        pre_close=number(4), volume=number(6),
        amount=amount if amount is not None else number(37) * 10000,
        turnover=number(38), timestamp=timestamp, fetched_at=utc_now(), source="腾讯",
        float_shares=_to_float(fields[73]) if len(fields) > 73 else None,
    )


def _fetch_quote_eastmoney(symbol: str) -> Optional[Quote]:
    secid = symbol_to_secid(symbol)
    params = {
        "secid": secid,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170,f168,f85,f86,f124",
        "fltt": "2",
        "invt": "2",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    data = _get_json_eastmoney("/api/qt/stock/get", params, QUOTE_HOSTS)
    if data and data.get("data"):
        d = data["data"]
        if d.get("f57", symbol) != symbol:
            raise ValueError("报价品种与请求不一致")
        q = Quote(
            symbol=symbol,
            name=d.get("f58", ""),
            price=_to_float(d.get("f43")) or 0,
            pct=_to_float(d.get("f170")) or 0,
            change=_to_float(d.get("f169")) or 0,
            high=_to_float(d.get("f44")) or 0,
            low=_to_float(d.get("f45")) or 0,
            open=_to_float(d.get("f46")) or 0,
            pre_close=_to_float(d.get("f60")) or 0,
            volume=_to_float(d.get("f47")) or 0,
            amount=_to_float(d.get("f48")) or 0,
            turnover=_to_float(d.get("f168")) or 0,
            timestamp=_quote_timestamp(d.get("f86")) or _quote_timestamp(d.get("f124")),
            fetched_at=utc_now(),
            float_shares=_to_float(d.get("f85")),
        )
        return q
    return None


# ---- 资金流 ----
@dataclass
class FundFlow:
    date: str
    main_net: float
    super_large_net: float
    large_net: float
    medium_net: float
    small_net: float
    main_pct: float = 0.0
    fetched_at: str = ""
    source: str = "东方财富"


@data_stage
def fetch_fund_flow(symbol: str, days: int = 30, force: bool = False) -> List[FundFlow]:
    """历史资金流。东财fflow/daykline。"""
    cache_key = f"flow_{symbol}_{days}"
    cached = _cached(cache_key)
    if cached and not force:
        return cached

    secid = symbol_to_secid(symbol)
    params = {
        "lmt": str(days),
        "klt": "101",
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    data = _get_flow_json("/api/qt/stock/fflow/daykline/get", params, [
        "https://push2his.eastmoney.com", "https://82.push2his.eastmoney.com",
        "https://push2test.eastmoney.com"])
    if data and data.get("data") and data["data"].get("klines"):
        flows: List[FundFlow] = []
        for line in data["data"]["klines"]:
            parts = line.split(",")
            if len(parts) >= 7:
                f = FundFlow(
                    date=parts[0],
                    main_net=float(parts[1]),
                    small_net=float(parts[2]),
                    medium_net=float(parts[3]),
                    large_net=float(parts[4]),
                    super_large_net=float(parts[5]),
                    main_pct=float(parts[6]) if parts[6] else 0.0,
                    fetched_at=data.get("fetched_at", ""),
                )
                flows.append(f)
        flows.sort(key=lambda flow: flow.date)
        if flows:
            _set_cache(cache_key, flows)
            return flows
    return []


# ---- 盘中实时分时资金流 ----
# 同一供应商的不同节点；push2 默认使用上方的旧版兼容解析。
RT_FLOW_HOSTS = [
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://82.push2.eastmoney.com",
]

# 实时数据缓存，2秒——保证秒级刷新拿到最新数据
_RT_CACHE_TTL = 2


@dataclass
class MinuteFlow:
    """盘中分时资金流（累计值）。"""
    time: str               # "09:31"
    main_net: float         # 累计主力净流入(元)
    small_net: float        # 累计小单净流入(元)
    medium_net: float       # 累计中单净流入(元)
    large_net: float        # 累计大单净流入(元)
    super_large_net: float  # 累计超大单净流入(元)
    date: str = ""
    fetched_at: str = ""
    source: str = "东方财富"
    quality_note: str = ""
    available: bool = True


@data_stage
def fetch_realtime_flow(symbol: str) -> List[MinuteFlow]:
    """腾讯优先、东财备用；每次采用一个来源的整组分钟累计序列，保留缺口。"""
    cache_key = f"rt_flow_{symbol}"
    cached = _cache_get(cache_key, _RT_CACHE_TTL)
    if cached is not None:
        return cached

    failures, unavailable, candidates = [], [], []
    sources = (
        ("腾讯", _fetch_realtime_flow_tencent, FLOW_PRIMARY_BUDGET_SECONDS),
        ("东方财富", _fetch_realtime_flow_eastmoney, HTTP_ATTEMPT_SECONDS),
    )
    for source, fetcher, budget in sources:
        try:
            with deadline_scope(time.monotonic() + budget):
                flows = fetcher(symbol)
            if flows:
                flows.sort(key=lambda flow: (flow.date, flow.time))
                latest_date = flows[-1].date
                if latest_date:
                    flows = [flow for flow in flows if flow.date == latest_date]
                candidates.append(flows)
                if (len(candidates) == 1 and flow_session_status(latest_date)["session_state"] != "awaiting_today"
                        and all(flow.available for flow in flows)):
                    _cache_set(cache_key, flows)
                    return flows
                continue
            unavailable.append(f"{source}未提供该品种的分时资金流记录")
        except (requests.RequestException, OSError, ValueError) as exc:
            reason = _source_error(exc) if isinstance(exc, requests.RequestException) else str(exc)
            failures.append(f"{source}：{reason}")
            log.warning("flow_source_failed symbol=%s source=%s error=%s", symbol, source, exc)
    if candidates:
        flows = max(candidates, key=lambda rows: (rows[-1].date, rows[-1].time,
                                                  rows[-1].available, -sum(not row.available for row in rows)))
        _cache_set(cache_key, flows)
        return flows
    if failures:
        raise OSError("；".join(failures + unavailable))
    _cache_set(cache_key, [])
    return []


def _fetch_realtime_flow_tencent(symbol: str) -> List[MinuteFlow]:
    code = f"bj{symbol}" if symbol.startswith("920") else symbol_to_tencent(symbol)
    response = _http_get(TENCENT_FLOW, params={"code": code, "type": "todayFundTrend"}).json()
    if (not isinstance(response, dict) or response.get("code") != 0
            or not isinstance(response.get("data"), dict)):
        raise ValueError("资金流接口未返回有效数据")
    trend = response["data"].get("todayFundTrend")
    if trend is None:
        return []
    if not isinstance(trend, dict) or trend.get("stockCode") != code:
        raise ValueError("资金流品种与请求不一致")
    rows = trend.get("minList")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("分时资金流记录格式无效")
    fetched_at = utc_now()
    flows = []
    fields = {"main_net": "MainNetInflow", "super_large_net": "SuperNetInflow",
              "large_net": "BigNetInflow", "medium_net": "NormalNetInflow",
              "small_net": "SmallNetInflow"}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("分时资金流记录格式无效")
        stamp = row.get("time")
        if not isinstance(stamp, str) or len(stamp) != 12 or not stamp.isdigit():
            raise ValueError("分时资金流日期格式无效")
        moment = datetime.strptime(stamp, "%Y%m%d%H%M")
        amounts = {name: _to_float(row.get(field)) for name, field in fields.items()}
        if any(value is None or not math.isfinite(value) for value in amounts.values()):
            raise ValueError("分时资金流金额缺失或无效")
        component_net = amounts["super_large_net"] + amounts["large_net"]
        quality_note = ""
        available = True
        if not math.isclose(amounts["main_net"], component_net, rel_tol=1e-5, abs_tol=1):
            if amounts["main_net"] != 0:
                raise ValueError("分时主力净额与超大单、大单之和不一致")
            # 金额加总一致不能证明分项的分钟时点；零占位不得用重复分项补值。
            available = False
            quality_note = "原始主力字段为零且与分项不一致，该分钟金额不可核验，未用分项补值"
        # 上游字段已是累计净额，单位为元；不再次累加，也不混入东财分项。
        flows.append(MinuteFlow(time=moment.strftime("%H:%M"), date=moment.date().isoformat(),
                                fetched_at=fetched_at, source="腾讯", quality_note=quality_note,
                                available=available, **amounts))
    return flows


def _fetch_realtime_flow_eastmoney(symbol: str) -> List[MinuteFlow]:
    secid = symbol_to_secid(symbol)
    params = {
        "klt": "1",  # 1分钟
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "lmt": "300",
    }

    data = _get_flow_json("/api/qt/stock/fflow/kline/get", params, RT_FLOW_HOSTS)
    if not data or data.get("data") is None:
        raise OSError("实时资金流数据源请求失败或未返回有效响应")
    if not data["data"].get("klines"):
        return []

    flows: List[MinuteFlow] = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        if len(parts) < 6:
            raise ValueError("东方财富分时资金流字段不足")
        moment = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
        amounts = [_to_float(value) for value in parts[1:6]]
        if any(value is None or not math.isfinite(value) for value in amounts):
            raise ValueError("东方财富分时资金流金额缺失或无效")
        flows.append(MinuteFlow(
            time=moment.strftime("%H:%M"), date=moment.date().isoformat(),
            main_net=amounts[0], small_net=amounts[1], medium_net=amounts[2],
            large_net=amounts[3], super_large_net=amounts[4],
            fetched_at=data.get("fetched_at", ""),
        ))
    if flows and get_instrument(symbol).get("benchmark") == "纳斯达克100":
        latest = max(row.date for row in flows)
        opening = [row for row in flows if row.date == latest and "09:31" <= row.time <= "10:30"]
        if len(opening) == 60 and all(not any((r.main_net, r.small_net, r.medium_net, r.large_net, r.super_large_net)) for r in opening):
            # 已核验的东财首小时零占位；不能把它画成真实的零流入。
            for row in opening:
                row.available = False
                row.quality_note = "东方财富09:31–10:30返回全零占位，首小时数据未核验，按缺失展示；未与其他来源拼接"
    return flows


# ---- 搜索 ----
@data_stage
def search_stock(keyword: str, count: int = 10) -> List[Dict]:
    """搜索股票。东财suggest接口。"""
    if not keyword or len(keyword.strip()) < 1:
        return []

    keyword = keyword.strip()
    cache_key = f"search_{keyword}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    # 纯数字6位代码直接返回
    if keyword.isdigit() and len(keyword) == 6:
        if keyword.startswith("6") or keyword.startswith("5"):
            market = "SH"
        elif keyword.startswith("920"):
            market = "BJ"
        else:
            market = "SZ"
        result = [{"code": keyword, "name": "", "market": market}]
        _set_cache(cache_key, result)
        return result

    params = {"input": keyword, "type": "14", "count": str(count), "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
    try:
        # 用urllib而非requests，避免某些环境下API返回JSONP格式
        import urllib.request, urllib.parse
        query = urllib.parse.urlencode(params)
        url = f"{SEARCH_HOST}/api/suggest/get?{query}"
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_POOL[0],
            "Referer": "https://quote.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=remaining_seconds(HTTP_ATTEMPT_SECONDS)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        log.warning(f"搜索失败: {e}")
        return []

    items = body.get("QuotationCodeTable", {}).get("Data") or []
    results = []
    for item in items:
        code = item.get("Code", "")
        name = item.get("Name", "")
        classify = item.get("Classify", "")
        # A股 + ETF + 北交所
        is_valid = classify in ("AStock", "Fund") or (len(code) == 6 and code[0] in "036") or code.startswith("920") or (len(code) == 6 and code.startswith("5"))
        if is_valid and len(code) == 6:
            if code.startswith("6") or code.startswith("5"):
                mkt = "SH"
            elif code.startswith("920"):
                mkt = "BJ"
            else:
                mkt = "SZ"
            results.append({"code": code, "name": name, "market": mkt})
    _set_cache(cache_key, results[:count])
    return results[:count]


# ---- 分时数据 ----
@dataclass
class MinuteData:
    times: List[str]       # 时间标签 "09:30"
    prices: List[float]    # 价格
    avg_prices: List[float] # 均价
    volumes: List[float]   # 成交量
    pre_close: float = 0.0
    name: str = ""
    high: float = 0.0
    low: float = 0.0


@data_stage
def fetch_minute(symbol: str) -> Optional[MinuteData]:
    """获取当日分时数据。东财trends2/get。"""
    cache_key = f"minute_{symbol}"
    cached = _cache_get(cache_key, _RT_CACHE_TTL)  # 用5秒短缓存，保证实时性
    if cached:
        return cached

    secid = symbol_to_secid(symbol)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "isccr": "1",
        "ndays": "1",
        "iscca": "0",
        "klt": "5",
        "fqt": "1",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    data = _get_json_eastmoney("/api/qt/stock/trends2/get", params, QUOTE_HOSTS)
    if not data or not data.get("data"):
        return None

    d = data["data"]
    trends = d.get("trends", [])
    if not trends:
        return None

    times, prices, avg_prices, volumes = [], [], [], []
    high, low = 0.0, 999999.0
    for line in trends:
        parts = line.split(",")
        if len(parts) >= 8:
            t = parts[0].split(" ")[-1] if " " in parts[0] else parts[0]
            price = float(parts[2]) if parts[2] else 0.0
            avg = float(parts[7]) if parts[7] else 0.0
            vol = float(parts[5]) if parts[5] else 0.0
            times.append(t)
            prices.append(price)
            avg_prices.append(avg)
            volumes.append(vol)
            if price > 0:
                high = max(high, price)
                low = min(low, price)

    md = MinuteData(
        times=times, prices=prices, avg_prices=avg_prices, volumes=volumes,
        pre_close=float(d.get("preClose", 0) or 0),
        name=d.get("name", ""),
        high=high if high > 0 else 0.0,
        low=low if low < 999999 else 0.0,
    )
    _set_cache(cache_key, md)
    return md


# ---- 大盘指数 ----
@data_stage
def fetch_index_kline(index_code: str = "000001", count: int = 60) -> List[Kline]:
    """获取大盘指数K线数据（上证指数/深证成指等）。

    index_code:
      - "000001" → 上证指数 (secid=1.000001, 腾讯=sh000001)
      - "399001" → 深证成指 (secid=0.399001, 腾讯=sz399001)
      - "399006" → 创业板指 (secid=0.399006, 腾讯=sz399006)
    """
    cache_key = f"index_{index_code}_{count}"
    cached = _cached(cache_key)
    if cached:
        return cached

    # 指数的secid: 上证=1.000001, 深证/创业板=0.399xxx
    if index_code.startswith("399"):
        secid = f"0.{index_code}"
    else:
        secid = f"1.{index_code}"

    # 指数无前复权概念(fqt=0)，用EM_KLINE_HOSTS(push2his优先+空klines检测)
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "lmt": str(count + 1),
        "end": "20500101",
    }

    data = _get_json_eastmoney("/api/qt/stock/kline/get", params, EM_KLINE_HOSTS)
    if not data or not data.get("data") or not data["data"].get("klines"):
        # 东财失败 → 腾讯尝试
        klines = exclude_preopen_bar(_fetch_kline_tencent_index(index_code, count + 1))[-count:]
        if klines:
            _set_cache(cache_key, klines)
            return klines
        return []

    klines: List[Kline] = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        if len(parts) >= 7:
            k = Kline(
                date=parts[0],
                open=float(parts[1]),
                close=float(parts[2]),
                high=float(parts[3]),
                low=float(parts[4]),
                volume=float(parts[5]) if parts[5] else 0.0,
                amount=_to_float(parts[6]) or 0.0,
            )
            if len(parts) >= 11:
                k.turnover = _to_float(parts[10]) or 0.0
            if klines:
                k.pct = round((k.close - klines[-1].close) / klines[-1].close * 100, 2)
            klines.append(k)

    klines = exclude_preopen_bar(klines)[-count:]
    if len(klines) >= 10:
        _set_cache(cache_key, klines)
        return klines
    return []


def _fetch_kline_tencent_index(index_code: str, count: int) -> List[Kline]:
    """腾讯API获取指数K线（上证/深证/创业板）。"""
    if index_code.startswith("399"):
        tc_symbol = f"sz{index_code}"
    else:
        tc_symbol = f"sh{index_code}"

    params = {"param": f"{tc_symbol},day,,,{count},"}
    try:
        r = _http_get(TENCENT_KLINE, params=params)
        text = r.text.strip()
        if text.startswith("<!DOCTYPE") or text.startswith("<html"):
            return []
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            return []
        if data.get("code") != 0:
            return []

        stock_data = data.get("data", {}).get(tc_symbol, {})
        # 指数没有qfq/day，只有day
        rows = stock_data.get("day", [])
        klines: List[Kline] = []
        for row in rows:
            if len(row) >= 6:
                vol = float(row[5]) if isinstance(row[5], (str, int, float)) else 0.0
                k = Kline(
                    date=str(row[0]),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=vol,
                )
                if klines:
                    k.pct = round((k.close - klines[-1].close) / klines[-1].close * 100, 2)
                klines.append(k)
        return klines
    except (requests.RequestException, ValueError) as e:
        log.error(f"腾讯指数K线失败 {index_code}: {e}")
        return []


# ---- 市场宽度（涨跌家数）----
def fetch_market_breadth() -> Optional[dict]:
    """兼容扫描调用；此函数始终只读，不触发网络请求。"""
    from data.market_breadth import market_breadth_cache
    snapshot = market_breadth_cache.snapshot()
    return snapshot["data"] if snapshot["usable_for_scoring"] else None


_breadth_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="breadth-page")


@data_stage
def _fetch_breadth_page(page: int) -> dict:
    base_params = {
        "po": "1",
        "np": "1",
        "fltt": "2",
        "fields": "f3,f12,f13",
        "fid": "f12",  # 固定代码排序，避免涨跌幅变化造成跨页重复/漏项。
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "pz": "100",
        "pn": str(page),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        response = _get_json_eastmoney("/api/qt/clist/get", base_params, [
            "https://push2.eastmoney.com", "https://82.push2.eastmoney.com",
            "https://push2delay.eastmoney.com"], strict=True)
    except OSError as exc:
        raise OSError(f"市场宽度第{page}页获取失败：{exc}") from exc
    data = response.get("data") if response else None
    if not data or not data.get("diff"):
        raise OSError(f"市场宽度第{page}页获取失败")
    return data


def _collect_market_breadth_eastmoney() -> dict:
    """仅后台调用。全部分页成功且完整后才发布新结果。"""
    first_page = _fetch_breadth_page(1)
    total_stocks = int(first_page.get("total", 0))
    if not 100 <= total_stocks <= 10000:
        raise ValueError(f"市场宽度总数异常: {total_stocks}")
    total_pages = (total_stocks + 99) // 100
    pages = {"1": first_page}
    if total_pages > 1:
        jobs = {str(page): lambda page=page: _fetch_breadth_page(page)
                for page in range(2, total_pages + 1)}
        results, errors, _ = collect_stages(jobs, required=jobs, budget=30, executor=_breadth_executor)
        if errors:
            raise OSError(f"市场宽度分页失败: {errors}")
        pages.update(results)
    up = down = flat = 0
    seen = set()
    for page, data in pages.items():
        rows = data["diff"]
        expected = min(100, total_stocks - (int(page) - 1) * 100)
        if data.get("total") != total_stocks or len(rows) != expected:
            raise ValueError(f"市场宽度第{page}页不完整")
        for row in rows:
            identity = (row["f13"], row["f12"])
            if identity in seen:
                raise ValueError("市场宽度分页出现重复股票")
            seen.add(identity)
            pct = _to_float(row.get("f3"))
            if pct is None or pct == 0:
                flat += 1
            elif pct > 0:
                up += 1
            else:
                down += 1
    total = up + down + flat
    breadth_ratio = up / max(up + down, 1) if (up + down) > 0 else 0.5
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "total": total,
        "breadth_ratio": round(breadth_ratio, 3),
        "source": "东方财富", "scope": "沪深京A股",
    }


SINA_MARKET_API = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."


def _fetch_sina_breadth_count() -> int:
    total = int(_http_get(SINA_MARKET_API + "getHQNodeStockCount", params={"node": "hs_a"}).json())
    if not 100 <= total <= 10000:
        raise ValueError(f"新浪市场宽度总数异常：{total}")
    return total


@data_stage
def _fetch_sina_breadth_page(page: int) -> list:
    rows = _http_get(SINA_MARKET_API + "getHQNodeData", params={
        "node": "hs_a", "num": 100, "page": page, "sort": "symbol", "asc": 1,
    }).json()
    if not isinstance(rows, list):
        raise ValueError(f"新浪市场宽度第{page}页格式无效")
    return rows


def _collect_market_breadth_sina() -> dict:
    total = _fetch_sina_breadth_count()
    jobs = {str(page): lambda page=page: _fetch_sina_breadth_page(page)
            for page in range(1, (total + 99) // 100 + 1)}
    pages, errors, _ = collect_stages(jobs, required=jobs, budget=15, executor=_breadth_executor)
    if errors:
        raise OSError(f"新浪市场宽度分页失败：{errors}")
    counts = {"up": 0, "down": 0, "flat": 0, "unknown": 0}
    seen = set()
    for page, rows in pages.items():
        if len(rows) != min(100, total - (int(page) - 1) * 100):
            raise ValueError(f"新浪市场宽度第{page}页不完整")
        for row in rows:
            symbol = row.get("symbol")
            if not symbol or symbol in seen:
                raise ValueError("新浪市场宽度股票代码缺失或跨页重复")
            seen.add(symbol)
            pct = _to_float(row.get("changepercent"))
            key = "unknown" if pct is None or not math.isfinite(pct) else "up" if pct > 0 else "down" if pct < 0 else "flat"
            counts[key] += 1
    if _fetch_sina_breadth_count() != total:
        raise ValueError("新浪市场宽度采集期间股票总数变化，等待下次完整刷新")
    moving = counts["up"] + counts["down"]
    return {**counts, "total": total, "breadth_ratio": round(counts["up"] / moving, 3) if moving else 0.5,
            "source": "新浪", "scope": "沪深京A股（新浪hs_a股票池）"}


def collect_market_breadth() -> dict:
    """独立供应商回退，每次只发布一个来源的完整股票池。"""
    failures = []
    for source, fetcher, budget in (("新浪", _collect_market_breadth_sina, 15),
                                     ("东方财富", _collect_market_breadth_eastmoney, 15)):
        try:
            with deadline_scope(time.monotonic() + budget):
                result = fetcher()
            result["source_failures"] = failures
            return result
        except (requests.RequestException, OSError, ValueError) as exc:
            failures.append(f"{source}：{exc}")
            log.warning("breadth_source_failed source=%s error=%s", source, exc)
    raise OSError("；".join(failures))


def fetch_all_a_shares() -> List[Dict]:
    """获取全A股列表（代码+名称+价格+涨跌幅+成交额）。

    用于扫描功能预过滤。全量抓取~5800只，10线程并发，约0.5秒完成。
    60秒缓存。
    """
    cache_key = "all_a_shares"
    cached = _cache_get(cache_key, 60)
    if cached:
        return cached

    import concurrent.futures

    CLIST_PATH = "/api/qt/clist/get"
    base_params = {
        "po": "1",
        "np": "1",
        "fltt": "2",
        "fields": "f2,f3,f6,f12,f14",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "pz": "100",
    }

    def _fetch_page(pn: int) -> tuple:
        """返回 (page_data, total)"""
        params = dict(base_params, pn=str(pn))
        s = _get_session()
        for host in QUOTE_HOSTS:
            try:
                url = host + CLIST_PATH
                r = s.get(url, params=params, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    if data and data.get("data"):
                        diff = data["data"].get("diff") or []
                        total = data["data"].get("total", 0)
                        return diff, total
            except Exception:
                continue
        return [], 0

    # 第一页拿总数
    first_diff, total_stocks = _fetch_page(1)
    if not first_diff:
        log.warning("获取A股列表失败: 第一页为空")
        return []

    total_pages = (total_stocks + 99) // 100 if total_stocks else 59
    log.info(f"A股列表: {total_stocks}只, {total_pages}页, 开始全量抓取")

    all_stocks = []

    def _parse_diff(diff: list) -> list:
        result = []
        for d in diff:
            code = str(d.get("f12", "")).strip()
            name = str(d.get("f14", "")).strip()
            price = _to_float(d.get("f2")) or 0
            pct = _to_float(d.get("f3")) or 0
            amount = _to_float(d.get("f6")) or 0
            if code and len(code) == 6:
                result.append({
                    "code": code,
                    "name": name,
                    "price": price,
                    "pct": pct,
                    "amount": amount,
                })
        return result

    all_stocks.extend(_parse_diff(first_diff))

    if total_pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_page, pn) for pn in range(2, total_pages + 1)]
            for f in concurrent.futures.as_completed(futures):
                diff, _ = f.result()
                all_stocks.extend(_parse_diff(diff))

    log.info(f"A股列表获取完成: {len(all_stocks)}只")
    _cache_set(cache_key, all_stocks)
    return all_stocks
