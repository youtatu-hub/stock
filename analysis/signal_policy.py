"""统一应用风控，并从最终决策生成摘要和操作计划。"""
from __future__ import annotations

import logging
import math

log = logging.getLogger("trend_app")
BUY_ACTIONS = ("买入", "强烈买入", "谨慎买入")
HARD_VETO = (
    ("跌破MA20", "价格跌破MA20，趋势已坏"),
    ("价跌量增", "价跌量增，恐慌抛售信号"),
    ("OBV下降", "OBV下降，量能走弱"),
    ("OBV走低", "OBV走低，量能走弱"),
    ("OBV下行", "OBV下行，量能走弱"),
)
SOFT_VETO = (
    ("MA20向下", "MA20向下，短期趋势偏弱"),
    ("MA20下行", "MA20下行，短期趋势偏弱"),
    ("受压60日", "受压60日决策线，上方压力大"),
)


def apply_signal_optimization(signal_data: dict, klines: list, quote) -> dict:
    """硬否决和盈亏比决定能否入场；大盘评分只降低评级和仓位。"""
    action = original_action = signal_data.get("action", "观望")
    score = signal_data.get("score", 0)
    confidence = signal_data.get("confidence", 0)
    module_scores = signal_data.get("module_scores", {})
    trend = signal_data.get("trend") or {}
    vp = signal_data.get("volume_price") or {}
    m_score = (signal_data.get("canslim") or {}).get("m_score", 50)
    plan = dict(signal_data.get("trade_plan") or {})
    reasons, risk_notes = [], []
    stock_signals = list(trend.get("signals", [])) + list(vp.get("signals", []))
    for text in signal_data.get("buy_signals", []) + signal_data.get("sell_signals", []):
        if not any(word in text for word in ("大盘", "市场环境", "空头环境", "今日", "上证")):
            stock_signals.append(text)
    stock_signals.append(vp.get("pattern", ""))
    signal_text = " ".join(stock_signals).replace(" ", "")
    hard_reason = next((reason for key, reason in HARD_VETO if key in signal_text), None)
    soft_reason = next((reason for key, reason in SOFT_VETO if key in signal_text), None)
    modules_above_55 = sum(module_scores.get(key, 50) >= 55
                           for key in ("趋势", "CAN_SLIM", "突破", "量价", "形态"))

    if action in BUY_ACTIONS:
        if hard_reason:
            action = "观望"
            reasons.append(f"硬否决：{hard_reason}")
        else:
            if score >= 75 and confidence >= 60 and modules_above_55 >= 4:
                action = "强烈买入"
            elif score >= 65 and confidence >= 45 and modules_above_55 >= 3:
                action = "买入"
            elif score >= 60:
                action = "谨慎买入"
            else:
                action = "观望"
            if soft_reason and action in ("强烈买入", "买入"):
                action = {"强烈买入": "买入", "买入": "谨慎买入"}[action]
                reasons.append(f"软否决：{soft_reason}")

    # 用实际展示价重新计算，不能相信过时或舍入后的比值，也不能跳过零收益。
    entry, stop, target = (plan.get(key) or 0 for key in ("entry_price", "stop_loss", "target_price"))
    valid_prices = all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                       for value in (entry, stop, target)) and stop < entry
    raw_ratio = (target - entry) / (entry - stop) if valid_prices else None
    risk_reward = round(raw_ratio, 2) if raw_ratio is not None else None
    if raw_ratio is not None:
        if raw_ratio < 1.0 and not math.isclose(raw_ratio, 1.0, abs_tol=1e-9):
            reason = "预期收益小于潜在亏损（盈亏比<1），不建议入场"
            risk_notes.append(reason)
            if action in BUY_ACTIONS:
                action = "观望"
            if original_action in BUY_ACTIONS:
                reasons.append(reason)
        elif raw_ratio < 1.5:
            risk_notes.append(f"盈亏比{risk_reward}偏低，谨慎操作")
        elif raw_ratio < 2.0:
            risk_notes.append(f"盈亏比{risk_reward}，勉强达标")
        else:
            risk_notes.append(f"盈亏比{risk_reward}，风险收益比良好")
    elif action in BUY_ACTIONS:
        action = "观望"
        reasons.append("交易价位无效，无法核算风险收益")

    # 先完成所有否决，再产生唯一的最终仓位和计划。
    if action in BUY_ACTIONS:
        if m_score < 40:
            action = {"强烈买入": "买入", "买入": "谨慎买入"}.get(action, action)
            position = "轻仓(1/4) — 大盘偏空，严格控制仓位"
            reasons.append(f"大盘M分{m_score}偏低，限制评级和仓位")
        elif m_score < 55:
            position = "半仓(1/2) — 大盘中性偏弱"
        else:
            position = plan.get("position_size") or "半仓(1/2)"
            if position == "空仓等待":
                position = "半仓(1/2)"
    elif "卖出" in action:
        position = "减仓或离场"
    else:
        position = "空仓等待"
        if not reasons:
            reasons.append(f"综合评分{score}分，买入条件尚未满足")

    executable = action in BUY_ACTIONS
    plan.update(action=action, position_size=position, executable=executable,
                risk_reward_ratio=risk_reward if executable else None)
    if not executable:
        for key in ("entry_price", "stop_loss", "target_price", "max_loss_pct"):
            plan[key] = None
        plan["notes"] = "；".join(reasons)
    reason_text = "；".join(reasons)
    if executable:
        summary = (f"最终建议：{action}。建议{position}。"
                   f"买入价{entry:.3f}，止损{stop:.3f}，目标{target:.3f}"
                   f"（盈亏比{risk_reward}）。")
    else:
        summary = f"最终建议：{action}。{position}。"
    if reason_text:
        summary += f"原因：{reason_text}。"

    warnings = list(signal_data.get("risk_warnings", []))
    if reason_text and reason_text not in warnings:
        warnings.insert(0, reason_text)
    signal_data.update(
        action=action, optimized_action=action, original_action=original_action,
        veto_reason=reason_text, decision_reasons=reasons, risk_warnings=warnings,
        position_advice=position, risk_notes=risk_notes, risk_reward=risk_reward,
        trade_plan=plan, plain_summary=summary,
        market_policy="大盘评分影响综合评分、评级和仓位，不单独硬性否决个股买入信号。",
    )
    log.info("signal_policy original=%s final=%s score=%s M=%s reasons=%s",
             original_action, action, score, m_score, reason_text)
    return signal_data
