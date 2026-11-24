"""管理模型估算的收盘基准；不抓历史接口，不用现价补历史。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.qdii_estimate import (
    MARKET_SYMBOLS, baseline_store, capture_close_baseline, price_basis,
)
from data.us_stock_time import next_us_stock_close, us_stock_close


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="查看已有基准日期及下次收盘时间")
    actions.add_parser("capture", help="仅在当前收盘采集窗口抓取实时基准")
    template = actions.add_parser("template", help="输出指定净值日期的手工录入模板")
    template.add_argument("--date", required=True, type=date.fromisoformat)
    importer = actions.add_parser("import", help="导入已核实的收盘时点价格")
    importer.add_argument("--file", required=True, help="JSON文件路径；- 表示从标准输入读取")
    importer.add_argument("--replace", action="store_true", help="显式覆盖同日已有基准")
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            result = {"stored_dates": baseline_store.dates(), "database": str(baseline_store.path),
                      "next_close": next_us_stock_close(datetime.now(timezone.utc)).isoformat()}
        elif args.action == "capture":
            result = capture_close_baseline()
        elif args.action == "template":
            close = us_stock_close(args.date)
            if close is None:
                raise ValueError("该日不是美股交易日")
            result = {"nav_date": args.date.isoformat(), "close_time": close.isoformat(), "origin": "manual",
                      "source": "", "quotes": {
                          symbol: {"series": code, "price_basis": price_basis(symbol),
                                   "price": None, "time": close.isoformat()}
                          for symbol, code in MARKET_SYMBOLS.items()}}
        else:
            content = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
            record = json.loads(content)
            if not isinstance(record, dict):
                raise ValueError("基准文件必须为JSON对象")
            record.update(origin="manual", recorded_at=datetime.now(timezone.utc).isoformat())
            saved = baseline_store.save(record, replace=args.replace)
            result = {"status": "imported", "nav_date": saved["nav_date"], "origin": "manual"}
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
