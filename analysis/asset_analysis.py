"""场内基金价格参考；不套用个股评分或生成固定收益目标。"""
from __future__ import annotations

from .breakout_module import analyze_breakout
from .canslim_module import analyze_canslim
from .signal_engine import SignalEngineResult
from .trend_module import analyze_trend


def price_metrics(klines: list) -> dict:
    peak = klines[0].close
    drawdown = 0.0
    for bar in klines:
        peak = max(peak, bar.close)
        drawdown = min(drawdown, (bar.close / peak - 1) * 100)
    return {"start_date": klines[0].date, "end_date": klines[-1].date,
            "sample_count": len(klines), "max_drawdown_pct": round(drawdown, 2),
            "return_pct": round((klines[-1].close / klines[0].close - 1) * 100, 2),
            "basis": "当前加载区间的复权收盘价；周线回撤按周收盘计算，不能代替日线回撤或基金总回报。"}


def analyze_listed_fund(klines: list, instrument: dict, index_klines=None, flows=None) -> SignalEngineResult:
    trend = analyze_trend(klines)
    technical = instrument["price_models"]
    canslim = analyze_canslim(klines, flows=flows, index_klines=index_klines) if instrument["canslim"] else None
    if canslim and not flows:
        canslim.signals.append("日级资金流未提供，I维度使用中性占位")
    return SignalEngineResult(
        action="趋势参考" if technical else "资产观察", score=None, confidence=None, risk_level="",
        trend=trend, breakouts=analyze_breakout(klines) if technical else [],
        model=instrument["model"], asset_metrics=price_metrics(klines),
        canslim=canslim,
        plain_summary=f"{instrument['label']}：当前价格趋势{trend.direction}。{instrument.get('note', '')}",
        trade_plan={"executable": False},
        description="价格走势与区间回撤仅供参考，实际交易需结合基金规则和成本。",
    )
