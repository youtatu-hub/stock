"""仅输出耗时和数据条数，不保存行情。例：py -3 tools/benchmark_requests.py --runs 3"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from data import kline_fetcher
from data.request_runtime import request_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600000")
    parser.add_argument("--period", default="day", choices=("day", "week"))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--endpoint", choices=("analyze", "kline", "quote", "chanlun_daily"), default="analyze")
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    # 对比时确保市场宽度已由后台成功获取，避免用缺失数据换取表面加速。
    started = time.monotonic()
    app.market_breadth_cache.refresh()
    snapshot = app.market_breadth_cache.snapshot()
    print(json.dumps({"breadth_warmup_ms": round((time.monotonic() - started) * 1000, 1),
                      "available": snapshot["available"], "error": snapshot["last_error"]}, ensure_ascii=False), flush=True)
    handler = getattr(app, f"handle_{args.endpoint}")
    params = {"symbol": [args.symbol], "period": [args.period], "basic": ["1"], "count": ["250"]}
    for run in range(1, args.runs + 1):
        with kline_fetcher._cache_lock:
            kline_fetcher._cache.clear()
        for mode in ("cold", "warm"):
            token = request_id.set(f"benchmark-{run}-{mode}")
            started = time.monotonic()
            try:
                result = handler(params)
            finally:
                request_id.reset(token)
            print(json.dumps({
                "run": run, "mode": mode, "endpoint": args.endpoint,
                "symbol": args.symbol, "period": args.period,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "stages_ms": result.get("timings_ms"), "error": result.get("error"),
                "degraded": result.get("degraded", {}), "klines": len(result.get("klines", [])),
                "breadth_total": (result.get("breadth") or {}).get("total"),
            }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
