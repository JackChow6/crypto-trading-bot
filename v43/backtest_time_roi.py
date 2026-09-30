#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_time_roi.py —— 回测时间衰减止盈（方案 B，freqtrade minimal_roi 思路）。

止盈门槛随持仓时长衰减：
  - 持仓 0~60 分钟：浮盈 ≥ tp_usd 才止盈（正常）
  - 持仓 60~120 分钟：浮盈 ≥ roi_mid_usd 就止盈（降低门槛）
  - 持仓 >120 分钟：浮盈 ≥ roi_min_usd 就止盈（保本微利就跑）

止损保持动态 ATR 止损不变。对比固定止盈（无时间衰减）。
"""
from __future__ import annotations
import json, os, sys, time
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_optuna as bo

CONFIG = os.path.join(ROOT, "config.yaml")
NOTIONAL = 100.0
FEE_PCT = 0.05


def simulate_time_roi(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                      roi_mid_usd, roi_min_usd, mid_min=60, min_min=120):
    """时间衰减止盈：持仓越久，止盈门槛越低。bars 是 1m K 线，索引即分钟数。"""
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / NOTIONAL)

    if side == "long":
        sl = entry - sl_dist
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1  # 第 i 根 bar 结束时持仓 i+1 分钟
            # 时间衰减止盈门槛
            if minutes <= mid_min:
                roi = tp_usd
            elif minutes <= min_min:
                roi = roi_mid_usd
            else:
                roi = roi_min_usd
            tp = entry + entry * (roi / NOTIONAL)
            # 止损优先
            if l <= sl:
                return bo.pnl_of(side, entry, sl)
            # 止盈（按当前持仓时长的门槛）
            if h >= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        sl = entry + sl_dist
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            if minutes <= mid_min:
                roi = tp_usd
            elif minutes <= min_min:
                roi = roi_mid_usd
            else:
                roi = roi_min_usd
            tp = entry - entry * (roi / NOTIONAL)
            if h >= sl:
                return bo.pnl_of(side, entry, sl)
            if l <= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)


def simulate_fixed(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max):
    """固定止盈（对照组）。"""
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / NOTIONAL)
    tp_dist = entry * (tp_usd / NOTIONAL)
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        for ts, o, h, l, c in bars:
            if l <= sl:
                return bo.pnl_of(side, entry, sl)
            if h >= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        for ts, o, h, l, c in bars:
            if h >= sl:
                return bo.pnl_of(side, entry, sl)
            if l <= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)


def stats(pnls):
    n = len(pnls)
    total = sum(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    wr = len(wins)/n*100 if n else 0
    pf = sum(wins)/abs(sum(losses)) if losses else float("inf")
    return total, wr, pf


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    tp_usd = float(e.get("fixed_tp_usd", 2.19))
    sl_base = float(e.get("fixed_sl_usd", 4.29))
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 1.80))
    sl_min = float(e.get("sl_min_usd", 0.85))
    sl_max = float(e.get("sl_max_usd", 4.10))

    trades = bo.load_trades()
    bars_cache = bo.load_bars()
    sl_bars = bo.load_sl_bars()
    prepared = bo.prepare_trades(trades, bars_cache, sl_bars)
    print(f"回测 {len(prepared)} 笔（maker入场 + 1h ATR 止损）\n")

    # 固定止盈对照组
    fixed_pnls = [simulate_fixed(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max) for p in prepared]
    ft, fw, fp = stats(fixed_pnls)
    print(f"{'方案':<40}{'总盈亏':<12}{'胜率':<10}{'盈亏比'}")
    print("-" * 70)
    print(f"{'固定止盈(对照组)':<38}{ft:<+12.2f}{fw:<10.1f}{fp:<10.2f}")

    # 时间衰减止盈：扫描不同的 mid/min 门槛组合
    print("\n时间衰减止盈方案（持仓越久止盈门槛越低）：")
    combos = [
        ("1h后降1u, 2h后保本0.3u", 1.0, 0.3, 60, 120),
        ("1h后降1u, 2h后保本0.5u", 1.0, 0.5, 60, 120),
        ("0.5h后降1.5u, 1h后0.5u", 1.5, 0.5, 30, 60),
        ("0.5h后降1u, 1h后0.3u", 1.0, 0.3, 30, 60),
        ("1h后降1.5u, 3h后0.5u", 1.5, 0.5, 60, 180),
        ("2h后降1u, 4h后0.5u", 1.0, 0.5, 120, 240),
    ]
    best = None
    for label, mid, minu, mid_min, min_min in combos:
        pnls = [simulate_time_roi(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                  mid, minu, mid_min, min_min) for p in prepared]
        t, w, p = stats(pnls)
        print(f"{label:<38}{t:<+12.2f}{w:<10.1f}{p:<10.2f}")
        if best is None or t > best[1]:
            best = (label, t, w, p, mid, minu, mid_min, min_min)

    print("-" * 70)
    print(f"\n最优时间衰减方案：{best[0]}，总盈亏 {best[1]:+.2f}，胜率 {best[2]:.1f}%，盈亏比 {best[3]:.2f}")
    print(f"对比固定止盈：{ft:+.2f} → {best[1]:+.2f}（{best[1]-ft:+.2f}）")


if __name__ == "__main__":
    main()
