"""关注品种、资产类型和适用模型的唯一配置来源。"""
from __future__ import annotations

import re


ASSET_PROFILES = {
    "stock": {"label": "A股股票", "model": "stock", "chanlun": True},
    "equity_etf": {"label": "A股权益ETF", "model": "technical", "chanlun": True},
    "overseas_etf": {"label": "海外权益ETF", "model": "technical", "chanlun": True},
    "short_bond_etf": {"label": "短久期债券ETF", "model": "defensive", "chanlun": False},
    "long_bond_etf": {"label": "长久期国债ETF", "model": "defensive", "chanlun": False},
    "gold_etf": {"label": "黄金ETF", "model": "technical", "chanlun": True},
    "listed_fund": {"label": "场内基金（未细分）", "model": "reference", "chanlun": False},
    "fund": {"label": "场外联接基金", "model": "nav", "chanlun": False},
}
for asset_type, profile in ASSET_PROFILES.items():
    profile.update(canslim=profile["model"] == "stock",
                   price_models=profile["model"] in ("stock", "technical") or asset_type == "long_bond_etf",
                   fund_flow=profile["model"] != "nav")

GROUPS = (
    ("steady", "稳健压舱石"), ("core", "核心进攻"), ("dividend", "红利防御"),
    ("macro", "黄金与利率债"), ("us-premium", "美股溢价观察"), ("otc-etf", "场外联接 A/C"),
)
LISTED_FUND_PREFIXES = ("5", "15", "16", "18")


def _listed(code, name, group, asset_type, benchmark, note, *, version=1):
    return {"code": code, "name": name, "group": group, "asset_type": asset_type,
            "benchmark": benchmark, "note": note, "seed_version": version}


_LISTED = (
    _listed("511360", "短融ETF海富通", "steady", "short_bond_etf", "中证短融指数",
            "关注价格回撤和成交流动性；短债仍有利率、信用及交易价差风险。"),
    _listed("512050", "A500ETF华夏", "core", "equity_etf", "中证A500",
            "宽基权益观察；趋势与突破模型仅作价格参考。"),
    _listed("588000", "科创50ETF华夏", "core", "equity_etf", "科创50",
            "成长进攻观察，波动和回撤可能较大；日周趋势须针对本品种分别观察。", version=3),
    _listed("159915", "创业板ETF易方达", "core", "equity_etf", "创业板指",
            "成长进攻观察；全A股扫描结果不代表本ETF的日周共振。", version=3),
    _listed("512890", "红利低波ETF", "dividend", "equity_etf", "中证红利低波动",
            "红利低波仍属于权益资产，不能按现金或保本产品理解。"),
    _listed("518880", "黄金ETF华安", "macro", "gold_etf", "上海黄金交易所黄金现货",
            "观察黄金价格；黄金并不保证在每次权益回撤时上涨。", version=3),
    _listed("511090", "30年国债ETF鹏扬", "macro", "long_bond_etf", "中债30年期国债指数",
            "久期较长、对利率变化敏感；利率上行时可能明显回撤，不等同短融ETF。", version=3),
    _listed("513500", "标普500ETF博时", "us-premium", "overseas_etf", "标普500",
            "跨境交易存在时差、汇率及溢价风险；参考净值不保证可成交。"),
    _listed("513300", "纳斯达克ETF华夏", "us-premium", "overseas_etf", "纳斯达克100",
            "关注美股科技集中度、汇率和溢价；不使用A股市场环境评分。"),
    _listed("159501", "纳指ETF嘉实", "us-premium", "overseas_etf", "纳斯达克100",
            "跨境交易存在时差、汇率及溢价风险；参考净值不保证可成交。"),
    _listed("159941", "纳指ETF广发", "us-premium", "overseas_etf", "纳斯达克100",
            "跨境交易存在时差、汇率及溢价风险；参考净值不保证可成交。"),
    _listed("513100", "纳指ETF国泰", "us-premium", "overseas_etf", "纳斯达克100",
            "跨境交易存在时差、汇率及溢价风险；参考净值不保证可成交。"),
)

# 份额配对按人民币类别核验；代码相邻不代表同一币种或A/C关系。
FUND_PAIRS = (
    ("050025", "006075", "博时标普500ETF联接", "标普500"),
    ("015299", "015300", "华夏纳斯达克100ETF联接", "纳斯达克100"),
    ("016055", "016057", "博时纳斯达克100ETF联接", "纳斯达克100"),
    ("270042", "006479", "广发纳斯达克100ETF联接", "纳斯达克100"),
    ("040046", "014978", "华安纳斯达克100ETF联接", "纳斯达克100"),
)

INSTRUMENTS = {item["code"]: item for item in _LISTED}
for a_code, c_code, name, benchmark in FUND_PAIRS:
    for code, share_class, peer in ((a_code, "A", c_code), (c_code, "C", a_code)):
        INSTRUMENTS[code] = {
            "code": code, "name": f"{name}(QDII){share_class}", "group": "otc-etf",
            "asset_type": "fund", "benchmark": benchmark, "currency": "CNY",
            "share_class": share_class, "peer_code": peer,
            "seed_version": 2 if share_class == "A" else 3,
            "note": "按申购费、销售服务费及持有期赎回费比较A/C；规则以销售渠道和基金公告为准。",
        }


def get_instrument(symbol: str) -> dict:
    code = re.sub(r"^(sh|sz|bj)", "", symbol.lower())
    item = INSTRUMENTS.get(code)
    if item is None:
        # 未配置的场内基金不冒充股票，也不猜测其底层资产或跟踪基准。
        asset_type = "listed_fund" if code.startswith(LISTED_FUND_PREFIXES) else "stock"
        item = {"code": code, "name": "", "asset_type": asset_type, "benchmark": "",
                "note": "资产类别尚未细分，仅展示价格参考。" if asset_type == "listed_fund" else ""}
    profile = {**item, **ASSET_PROFILES[item["asset_type"]]}
    profile["canslim"] = profile["canslim"] or item.get("group") == "core"
    return profile


def instrument_catalog() -> dict:
    instruments = {code: get_instrument(code) for code in INSTRUMENTS}
    groups = [{"id": group_id, "name": name,
               "type": "fund" if group_id == "otc-etf" else "listed",
               "stocks": [item for item in instruments.values() if item.get("group") == group_id]}
              for group_id, name in GROUPS]
    return {"version": 3, "groups": groups, "instruments": instruments, "profiles": ASSET_PROFILES,
            "listed_fund_prefixes": LISTED_FUND_PREFIXES}
