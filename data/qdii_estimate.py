"""免费期货/外汇输入、按净值日期保存的收盘基准及定时采集。"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from analysis.qdii_estimate import (
    aware_time, calculate_estimate, fresh_quote, positive_number, unavailable_estimate,
)
from data.kline_fetcher import _cache_get, _cache_set, _http_get, _source_error, requests, utc_now
from data.request_runtime import data_stage
from data.us_stock_time import new_york_zone, next_us_stock_close, us_stock_close


log = logging.getLogger("qdii_estimate")
CHINA_TZ = timezone(timedelta(hours=8))
MARKET_URL = "https://hq.sinajs.cn/"
MARKET_SYMBOLS = {"NQ": "hf_NQ", "ES": "hf_ES", "USD/CNH": "fx_susdcnh"}
PROXY_BY_BENCHMARK = {"纳斯达克100": "NQ", "标普500": "ES"}
CACHE_SECONDS = 15
CLOSE_TOLERANCE_SECONDS = 30
CAPTURE_WINDOW_SECONDS = 60


def price_basis(symbol):
    return "spot_bid_ask_mid" if symbol == "USD/CNH" else "continuous_last"


def parse_market_quotes(text):
    if not isinstance(text, str):
        raise ValueError("模型报价响应必须是文本")
    rows = {}
    for name, body in re.findall(r'var\s+hq_str_(\w+)="([^"\r\n]*)"\s*;', text):
        if name in rows:
            raise ValueError("模型报价包含重复品种")
        rows[name] = body.split(",")
    result = {}
    for symbol, source_code in MARKET_SYMBOLS.items():
        fields = rows.get(source_code, [])
        if len(fields) < (18 if symbol == "USD/CNH" else 14):
            raise ValueError(f"来源未提供有效的 {symbol} 报价")
        if symbol == "USD/CNH":
            bid = positive_number(fields[1], "USD/CNH买报价")
            ask = positive_number(fields[2], "USD/CNH卖报价")
            if bid > ask:
                raise ValueError("USD/CNH买卖报价倒置")
            value, day, clock = positive_number((bid + ask) / 2, "USD/CNH即期买卖中值"), fields[17], fields[0]
        else:
            value, day, clock = positive_number(fields[0], f"{symbol}最新价"), fields[12], fields[6]
        stamp = datetime.strptime(day + " " + clock, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
        result[symbol] = {"symbol": symbol, "series": source_code, "price": value,
                          "price_basis": price_basis(symbol), "time": stamp.isoformat(),
                          "source": "新浪", "source_url": MARKET_URL + "list=" + source_code}
    return result


@data_stage
def fetch_estimate_market(force=False):
    cached = None if force else _cache_get("qdii_estimate_market", CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        # 此来源不解码批量符号中的 %2C；仅用固定品种白名单构造已核验的路径。
        response = _http_get(MARKET_URL + "list=" + ",".join(MARKET_SYMBOLS.values()),
                             headers={"Referer": "https://finance.sina.com.cn/"})
    except requests.RequestException as exc:
        raise OSError("新浪期货/汇率：" + _source_error(exc)) from exc
    result = parse_market_quotes(response.content.decode("gb18030"))
    fetched_at = utc_now()
    for quote in result.values():
        quote["fetched_at"] = fetched_at
    _cache_set("qdii_estimate_market", result)
    return result


def validate_baseline(record):
    if not isinstance(record, dict):
        raise ValueError("基准记录必须是JSON对象")
    if not isinstance(record.get("nav_date"), str):
        raise ValueError("基准净值日期必须为YYYY-MM-DD")
    day = date.fromisoformat(record["nav_date"])
    close = us_stock_close(day)
    if close is None:
        raise ValueError("基准净值日期不是已配置的美股交易日")
    if record.get("origin") not in ("manual", "scheduled"):
        raise ValueError("基准origin必须为manual或scheduled")
    source = record.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("基准必须填写来源说明")
    if aware_time(record.get("close_time"), "基准收盘时间") != close:
        raise ValueError("基准收盘时间不符合该日美股收盘日历")
    quotes = record.get("quotes")
    if not isinstance(quotes, dict) or set(quotes) != set(MARKET_SYMBOLS):
        raise ValueError("基准必须同时包含NQ、ES及USD/CNH")
    normalized = {}
    for symbol, source_code in MARKET_SYMBOLS.items():
        quote = quotes[symbol]
        if not isinstance(quote, dict) or quote.get("series") != source_code or quote.get("price_basis") != price_basis(symbol):
            raise ValueError(f"{symbol}基准必须与新浪当前报价的品种及价格口径一致")
        price = positive_number(quote.get("price"), f"{symbol}基准价")
        stamp = aware_time(quote.get("time"), f"{symbol}基准时间")
        if abs((stamp - close).total_seconds()) > CLOSE_TOLERANCE_SECONDS:
            raise ValueError(f"{symbol}基准偏离美股收盘时间超过{CLOSE_TOLERANCE_SECONDS}秒")
        normalized[symbol] = {**quote, "symbol": symbol, "price": price, "time": stamp.isoformat()}
    return {**record, "nav_date": day.isoformat(), "close_time": close.isoformat(), "quotes": normalized}


class BaselineStore:
    def __init__(self, path=None):
        directory = Path(os.environ.get("STOCK_ESTIMATE_DIR", str(Path(__file__).resolve().parents[1] / "runtime" / "qdii")))
        self.path = Path(path) if path is not None else directory / "baselines.sqlite3"

    def load(self, nav_date):
        key = date.fromisoformat(nav_date).isoformat()
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            row = connection.execute("SELECT payload FROM baselines WHERE nav_date = ?", (key,)).fetchone()
        return validate_baseline(json.loads(row[0])) if row else None

    def dates(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            return [row[0] for row in connection.execute("SELECT nav_date FROM baselines ORDER BY nav_date")]

    def save(self, record, *, replace=False):
        record = validate_baseline(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS baselines (nav_date TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            statement = "INSERT OR REPLACE INTO baselines VALUES (?, ?)" if replace else "INSERT INTO baselines VALUES (?, ?)"
            try:
                connection.execute(statement, (record["nav_date"], json.dumps(record, ensure_ascii=False, allow_nan=False)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该净值日期已有基准，默认不覆盖；核实后可显式使用--replace") from exc
        return record


baseline_store = BaselineStore()


def build_model_estimate(instrument, nav, market, quote=None, *, store=None, now=None):
    future = PROXY_BY_BENCHMARK.get(instrument.get("benchmark"))
    if future is None:
        return unavailable_estimate("unsupported", "该品种未配置期货代理")
    if not nav or not nav.get("history"):
        return unavailable_estimate("nav_missing", "缺少本ETF已披露净值，估算不可用")
    latest = nav["history"][-1]
    if not market:
        return unavailable_estimate("market_missing", "期货或汇率报价获取失败，估算不可用", nav_date=latest["date"])
    try:
        baseline = (store or baseline_store).load(latest["date"])
        if baseline is None:
            return unavailable_estimate("baseline_missing", f"缺少净值日 {latest['date']} 的收盘基准，请录入该日基准或等待后续采集。", nav_date=latest["date"])
        estimate = calculate_estimate(latest, baseline, market, future_symbol=future, quote=quote, now=now)
        estimate.update(nav_source=nav["source"], nav_source_url=nav["source_url"])
        return estimate
    except (OSError, sqlite3.Error, ValueError) as exc:
        log.warning("qdii_estimate_unavailable symbol=%s reason=%s", instrument["code"], exc)
        return unavailable_estimate("invalid_inputs", str(exc), nav_date=latest["date"])


def capture_close_baseline(store=None, *, fetcher=fetch_estimate_market, now=None):
    store = store or baseline_store
    started = now or datetime.now(timezone.utc)
    day = started.astimezone(new_york_zone()).date()
    close = us_stock_close(day)
    if close is None or not 0 <= (started - close).total_seconds() <= CAPTURE_WINDOW_SECONDS:
        raise ValueError("当前不在美股收盘后的60秒采集窗口，不能用现价回填历史基准")
    if store.load(day.isoformat()) is not None:
        return {"status": "exists", "nav_date": day.isoformat()}
    quotes = fetcher(force=True)
    received = now or datetime.now(timezone.utc)
    if not 0 <= (received - close).total_seconds() <= CAPTURE_WINDOW_SECONDS:
        raise ValueError("请求完成时已超过收盘采集窗口，本次数据不写入基准")
    for symbol in MARKET_SYMBOLS:
        fresh_quote(quotes.get(symbol), received, f"{symbol}收盘采样")
    record = {"nav_date": day.isoformat(), "close_time": close.isoformat(),
              "origin": "scheduled", "source": "新浪期货及USD/CNH即期报价，收盘附近采样",
              "captured_at": received.isoformat(), "quotes": quotes}
    store.save(record)
    return {"status": "captured", "nav_date": day.isoformat(), "close_time": close.isoformat()}


class CloseSnapshotService:
    def __init__(self, store=None, fetcher=fetch_estimate_market):
        self.store = store or baseline_store
        self.fetcher = fetcher
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="qdii-close-snapshot", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            delay = 30
            try:
                now = datetime.now(timezone.utc)
                close = us_stock_close(now.astimezone(new_york_zone()).date())
                if close and 0 <= (now - close).total_seconds() <= CAPTURE_WINDOW_SECONDS:
                    result = capture_close_baseline(self.store, fetcher=self.fetcher)
                    log.info("qdii_close_snapshot %s", result)
                    delay = CAPTURE_WINDOW_SECONDS
                else:
                    delay = min(30, max(1, (next_us_stock_close(now) - now).total_seconds()))
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("qdii_close_snapshot_failed: %s", exc)
                delay = 5
            self._stop.wait(delay)


close_snapshot_service = CloseSnapshotService()
