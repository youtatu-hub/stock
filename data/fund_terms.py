"""解析销售渠道公开费率表，不执行页面脚本，不将缺失值解释为免费或不限额。"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from .kline_fetcher import _cache_get, _cache_set, _http_get, utc_now
from .request_runtime import data_stage

TERMS_CACHE_SECONDS = 300


class _TermsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = ""
        self.heading_parts = None
        self.title_parts = None
        self.title = ""
        self.cell = None
        self.row = []
        self.in_table = False
        self.rows = []
        self.tables = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.title_parts = []
        elif tag == "h4":
            self.heading_parts = []
        elif tag == "table" and "jjfl" in dict(attrs).get("class", "").split():
            self.in_table = True
            self.rows = []
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag == "td":
            self.cell = []

    def handle_data(self, data):
        if self.title_parts is not None:
            self.title_parts.append(data)
        if self.heading_parts is not None:
            self.heading_parts.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self.title_parts is not None:
            self.title = "".join(self.title_parts)
            self.title_parts = None
        elif tag == "h4" and self.heading_parts is not None:
            self.heading = re.sub(r"\s+", "", "".join(self.heading_parts))
            self.heading_parts = None
        elif self.in_table and tag == "td" and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif self.in_table and tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []
        elif self.in_table and tag == "table":
            self.tables.append((self.heading, self.rows))
            self.in_table = False


def _present(value):
    return value if value and not re.fullmatch(r"[-—\s]+", value) else None


def parse_fund_terms(source: str, symbol: str) -> dict:
    parser = _TermsParser()
    parser.feed(source)
    if not re.search(r"\(" + re.escape(symbol) + r"\)", parser.title):
        raise ValueError("费率页面基金代码与请求不一致")
    values, fees = {}, {}
    for heading, rows in parser.tables:
        if heading in ("申购费率", "赎回费率"):
            fees[heading] = [{"condition": row[0], "rate": _present(row[1])}
                             for row in rows if len(row) == 2]
        elif heading != "认购费率":
            for row in rows:
                for index in range(0, len(row) - 1, 2):
                    values[row[index]] = _present(row[index + 1])
    if not values.get("申购状态") or not values.get("赎回状态"):
        raise ValueError("费率页面缺少申赎状态，无法确认交易规则")
    return {
        "symbol": symbol, "name": parser.title.split("(" + symbol + ")")[0].strip(),
        "trading": {"subscription": values.get("申购状态"), "redemption": values.get("赎回状态"),
                    "regular": values.get("定投状态"), "daily_limit": values.get("日累计申购限额"),
                    "minimum": values.get("申购起点"), "buy_confirmation": values.get("买入确认日"),
                    "sell_confirmation": values.get("卖出确认日")},
        "fees": {"subscription": fees.get("申购费率", []), "redemption": fees.get("赎回费率", []),
                 "management": values.get("管理费率"), "custody": values.get("托管费率"),
                 "sales_service": values.get("销售服务费率")},
    }


@data_stage
def fetch_fund_terms(symbol: str) -> dict:
    cached = _cache_get(f"fund_terms_{symbol}", TERMS_CACHE_SECONDS)
    if cached is not None:
        return cached
    url = f"https://fundf10.eastmoney.com/jjfl_{symbol}.html"
    response = _http_get(url)
    result = parse_fund_terms(response.content.decode(response.encoding or "utf-8"), symbol)
    result.update(source="天天基金（销售渠道）", source_url=url, fetched_at=utc_now(),
                  effective_date=None,
                  note="获取时间不等于规则生效时间；限额不代表当前开放申购，实际交易以销售渠道和基金公告为准。")
    _cache_set(f"fund_terms_{symbol}", result)
    return result
