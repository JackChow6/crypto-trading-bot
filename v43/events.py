#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/events.py —— Event Detector

只在「关键事件」发生时才唤醒 AI 分析/决策，绝不逐 tick 调模型。
每个事件带 type + 置信度 + 触发条件快照，供上层决定是否进入 Opportunity Gate。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Event:
    type: str            # BREAKOUT / BREAKDOWN / PULLBACK / MOMENTUM_SURGE / DELTA_FLIP / OI_SPIKE / FUNDING_EXTREME
    symbol: str
    confidence: float = 0.0   # 0~1，多条件命中越高
    note: str = ""
    meta: dict = field(default_factory=dict)


def detect(snapshot: dict, cfg: dict | None = None) -> list[Event]:
    """基于一次 Market Snapshot 检测事件。cfg 可调阈值。"""
    cfg = cfg or {}
    evs: list[Event] = []
    sym = snapshot.get("symbol", "?")
    vol_ratio = snapshot.get("vol_ratio") or 1.0
    ob_imbalance = (snapshot.get("orderbook") or {}).get("imbalance") or 0.0
    flow = snapshot.get("orderflow") or {}
    delta = flow.get("delta_1m") or 0.0
    cvd_dir = flow.get("cvd_direction", "FLAT")
    deriv = snapshot.get("derivatives") or {}
    oi_chg = deriv.get("oi_change_5m_pct") or 0.0
    funding = deriv.get("funding")
    rsi = snapshot.get("rsi14")

    vol_thr = float(cfg.get("volume_ratio_thr", 2.0))
    oi_thr = float(cfg.get("oi_change_thr", 2.0))
    funding_thr = float(cfg.get("funding_thr", 0.001))

    # 1) 突破 / 跌破
    if snapshot.get("breakout"):
        conf = 0.6
        if vol_ratio >= vol_thr:
            conf += 0.2
        if delta > 0 or cvd_dir == "RISING":
            conf += 0.1
        if oi_chg > 0:
            conf += 0.1
        evs.append(Event("BREAKOUT", sym, min(conf, 1.0),
                         f"突破阻力 {snapshot.get('resistance')}", {"vol_ratio": vol_ratio, "delta": delta}))
    if snapshot.get("breakdown"):
        conf = 0.6
        if vol_ratio >= vol_thr:
            conf += 0.2
        if delta < 0 or cvd_dir == "FALLING":
            conf += 0.1
        if oi_chg > 0:
            conf += 0.1
        evs.append(Event("BREAKDOWN", sym, min(conf, 1.0),
                         f"跌破支撑 {snapshot.get('support')}", {"vol_ratio": vol_ratio, "delta": delta}))

    # 2) 回踩：上升趋势中回踩支撑/EMA（做多入场信号，替代追突破）
    if snapshot.get("pullback"):
        evs.append(Event("PULLBACK", sym, 0.7,
                         f"回踩 {snapshot.get('pullback_level')}",
                         {"near_ema20": snapshot.get("near_ema20"), "near_support": snapshot.get("near_support")}))

    # 3) 动量突增（放量 + RSI 极端 + ATR 扩张）
    if vol_ratio >= vol_thr and (rsi is None or rsi >= 65 or rsi <= 35):
        evs.append(Event("MOMENTUM_SURGE", sym, 0.7,
                         f"放量 {vol_ratio:.1f}x RSI={rsi}", {"rsi": rsi}))

    # 4) Delta 方向翻转
    if delta > 0 and cvd_dir == "RISING" and ob_imbalance > 0.2:
        evs.append(Event("DELTA_FLIP", sym, 0.65, "主动买盘占优", {"delta": delta}))
    elif delta < 0 and cvd_dir == "FALLING" and ob_imbalance < -0.2:
        evs.append(Event("DELTA_FLIP", sym, 0.65, "主动卖盘占优", {"delta": delta}))

    # 5) OI 异动
    if abs(oi_chg) >= oi_thr:
        evs.append(Event("OI_SPIKE", sym, min(0.5 + abs(oi_chg) / 20, 1.0),
                         f"OI 5m 变化 {oi_chg:+.2f}%", {"oi_change": oi_chg}))

    # 6) 资金费率极端
    if funding is not None and abs(funding) >= funding_thr:
        evs.append(Event("FUNDING_EXTREME", sym, 0.6,
                         f"funding={funding:.5f}", {"funding": funding}))

    return evs
