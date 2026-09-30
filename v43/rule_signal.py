#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/rule_signal.py —— 确定性量化信号（替代 LLM 判断方向）

核心思路：顺势而为 + 多因子共振。只在「趋势方向明确且多指标一致」时才给信号，
震荡/分歧一律 NO_TRADE，从而过滤掉随机波动（50% 胜率的根源）。

方向判断采用「趋势三共振」：
  做多：Supertrend=多头 且 EMA20>EMA50 且 市场结构=UPTREND
  做空：Supertrend=空头 且 EMA20<EMA50 且 市场结构=DOWNTREND

入场事件（顺势触发）：
  做多：PULLBACK（回踩不追高）或 BREAKOUT（突破）
  做空：BREAKDOWN（跌破）

动量/量价确认（加分项，防追涨杀跌）：
  做多：taker_buy_ratio >= 0.5 或 CVD 趋势上升
  做空：taker_buy_ratio <= 0.5 或 CVD 趋势下降

置信度 = 命中条件数 / 总条件数 × 100（>= min_confidence 才放行）。
"""
from __future__ import annotations

import ai


def decide(snap: dict, evs: list, cfg: dict | None = None) -> ai.Decision:
    """返回 ai.Decision（action=LONG/SHORT/NO_TRADE + confidence + reason）。

    cfg 可覆盖阈值：trend_min_score(默认4), momentum_weight(是否要求量价确认)。
    """
    cfg = cfg or {}
    ev_types = [getattr(e, "type", e if isinstance(e, str) else "") for e in evs]
    st = snap.get("supertrend") or {}
    st_dir = st.get("direction") or 0
    ema20 = snap.get("ema20")
    ema50 = snap.get("ema50")
    structure = snap.get("structure")  # UPTREND / DOWNTREND / RANGE
    flow = snap.get("orderflow") or {}
    tbr = flow.get("taker_buy_ratio")
    cvd_trend = flow.get("cvd_trend", "FLAT")

    # 事件方向
    has_pullback = "PULLBACK" in ev_types
    has_breakout = "BREAKOUT" in ev_types
    has_breakdown = "BREAKDOWN" in ev_types

    # ---- 做多评分（5 项）----
    long_score = 0
    long_reasons = []
    if st_dir == 1:
        long_score += 1
        long_reasons.append("Supertrend多头")
    if ema20 is not None and ema50 is not None and ema20 > ema50:
        long_score += 1
        long_reasons.append("EMA20>EMA50")
    if structure == "UPTREND":
        long_score += 1
        long_reasons.append("结构UPTREND")
    if has_pullback or has_breakout:
        long_score += 1
        long_reasons.append("回踩/突破事件")
    if (tbr is not None and tbr >= 0.5) or cvd_trend == "RISING":
        long_score += 1
        long_reasons.append("买方主导")

    # ---- 做空评分（5 项）----
    short_score = 0
    short_reasons = []
    if st_dir == -1:
        short_score += 1
        short_reasons.append("Supertrend空头")
    if ema20 is not None and ema50 is not None and ema20 < ema50:
        short_score += 1
        short_reasons.append("EMA20<EMA50")
    if structure == "DOWNTREND":
        short_score += 1
        short_reasons.append("结构DOWNTREND")
    if has_breakdown:
        short_score += 1
        short_reasons.append("跌破事件")
    if (tbr is not None and tbr <= 0.5) or cvd_trend == "FALLING":
        short_score += 1
        short_reasons.append("卖方主导")

    # ---- 决策：趋势三共振是硬门槛（缺一不可），动量/事件是加分 ----
    # 硬门槛：Supertrend + EMA 排列 必须同时命中，否则方向不明朗
    trend_confirmed_long = (st_dir == 1) and (ema20 is not None and ema50 is not None and ema20 > ema50)
    trend_confirmed_short = (st_dir == -1) and (ema20 is not None and ema50 is not None and ema20 < ema50)

    trend_min = int(cfg.get("trend_min_score", 4))

    if trend_confirmed_long and long_score >= trend_min and (has_pullback or has_breakout):
        conf = round(long_score / 5 * 100)
        return ai.Decision(action="LONG", confidence=float(conf), risk_level=2,
                           bull="；".join(long_reasons), bear="",
                           reason=f"确定性趋势信号：{', '.join(long_reasons)}（评分{long_score}/5）")

    if trend_confirmed_short and short_score >= trend_min and has_breakdown:
        conf = round(short_score / 5 * 100)
        return ai.Decision(action="SHORT", confidence=float(conf), risk_level=2,
                           bull="", bear="；".join(short_reasons),
                           reason=f"确定性趋势信号：{', '.join(short_reasons)}（评分{short_score}/5）")

    return ai.Decision(action="NO_TRADE", confidence=0.0, risk_level=3,
                       bull="；".join(long_reasons), bear="；".join(short_reasons),
                       reason=f"趋势未共振或评分不足（多{long_score}/5 空{short_score}/5），观望")
