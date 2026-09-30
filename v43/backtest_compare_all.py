#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_compare_all.py —— 对比所有止盈止损设置的历史回测表现。

对比项：
  A. 纯固定止盈/止损（无任何衰减）
  B. 固定止盈 + 止盈时间衰减
  C. 固定止盈 + 止盈时间衰减 + 止损时间衰减（当前）
  D. 早期动态止盈（锁利继续跑）

统一用：maker入场 + 1h ATR 止损口径，118 笔实盘成交。
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


def simulate_fixed(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                   roi_mid=None, roi_min=None, mid_min=60, min_min=180,
                   sl_decay_mid=None, sl_decay_min=None, sl_mid_min=120, sl_min_min=240):
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    base_sl = sl_base
    if atr and entry:
        base_sl = atr * sl_atr_mult * NOTIONAL / entry
        base_sl = max(sl_min, min(sl_max, base_sl))

    def cur_sl(minutes):
        if sl_decay_mid is None:
            return base_sl
        if minutes <= sl_mid_min:
            return base_sl
        elif minutes <= sl_min_min:
            return sl_decay_mid
        return sl_decay_min

    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry - entry * (cur_sl(m) / NOTIONAL)
            if roi_mid is not None:
                roi = tp_usd if m <= mid_min else (roi_mid if m <= min_min else roi_min)
            else:
                roi = tp_usd
            tp = entry + entry * (roi / NOTIONAL)
            if l <= sl:
                return bo.pnl_of(side, entry, sl)
            if h >= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry + entry * (cur_sl(m) / NOTIONAL)
            if roi_mid is not None:
                roi = tp_usd if m <= mid_min else (roi_mid if m <= min_min else roi_min)
            else:
                roi = tp_usd
            tp = entry - entry * (roi / NOTIONAL)
            if h >= sl:
                return bo.pnl_of(side, entry, sl)
            if l <= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)


def simulate_dynamic(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                     trail1, trail2, trail2_after, be_ratio):
    """动态止盈：锁利继续跑（早期版本）。"""
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / NOTIONAL)
    tp_dist = entry * (tp_usd / NOTIONAL)
    be = sl_dist * be_ratio
    t1 = entry * (trail1 / NOTIONAL)
    t2 = entry * (trail2 / NOTIONAL)
    ta = entry * (trail2_after / NOTIONAL)

    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for ts, o, h, l, c in bars:
            if tp is not None and h >= tp:
                sl = max(sl, tp); tp = None
            eff = t2 if (highest >= entry + ta) else t1
            highest = max(highest, h)
            sl = max(sl, highest - eff)
            if highest >= entry + be:
                sl = max(sl, entry)
            if l <= sl:
                return bo.pnl_of(side, entry, sl)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for ts, o, h, l, c in bars:
            if tp is not None and l <= tp:
                sl = min(sl, tp); tp = None
            eff = t2 if (lowest <= entry - ta) else t1
            lowest = min(lowest, l)
            sl = min(sl, lowest + eff)
            if lowest <= entry - be:
                sl = min(sl, entry)
            if h >= sl:
                return bo.pnl_of(side, entry, sl)
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
    trades = bo.load_trades()
    bars_cache = bo.load_bars()
    sl_bars = bo.load_sl_bars()
    prepared = bo.prepare_trades(trades, bars_cache, sl_bars)
    print(f"对比回测（{len(prepared)} 笔实盘成交，maker入场 + 1h ATR 止损口径）\n")

    print(f"{'方案':<46}{'总盈亏':<12}{'胜率':<9}{'盈亏比'}")
    print("-" * 76)

    # A. 纯固定止盈（无衰减）
    a = [simulate_fixed(p, 2.19, 4.29, 1.80, 0.85, 4.10) for p in prepared]
    t, w, pf = stats(a)
    print(f"{'A 纯固定止盈2.19u/止损4.29u':<44}{t:<+12.2f}{w:<9.1f}{pf:<8.2f}")

    # B. 固定止盈 + 止盈时间衰减（之前寻优 +95.13 那套）
    b = [simulate_fixed(p, 2.19, 2.79, 1.88, 0.42, 4.10,
                        2.13, 0.57, 112, 213) for p in prepared]
    t, w, pf = stats(b)
    print(f"{'B 固定止盈+止盈衰减(112min→2.13,213min→0.57)':<44}{t:<+12.2f}{w:<9.1f}{pf:<8.2f}")

    # C. 固定止盈 + 止盈衰减 + 止损衰减（当前 +87.39 那套）
    c = [simulate_fixed(p, 2.20, 4.38, 1.94, 2.04, 5.24,
                        2.03, 0.12, 119, 340,
                        1.05, 0.27, 176, 337) for p in prepared]
    t, w, pf = stats(c)
    print(f"{'C 固定止盈+止盈衰减+止损衰减(当前)':<44}{t:<+12.2f}{w:<9.1f}{pf:<8.2f}")

    # D. 动态止盈（锁利继续跑，早期）
    d = [simulate_dynamic(p, 2.04, 2.53, 1.02, 1.71, 4.82,
                          2.83, 0.61, 7.84, 0.53) for p in prepared]
    t, w, pf = stats(d)
    print(f"{'D 动态止盈(锁利继续跑，早期版本)':<44}{t:<+12.2f}{w:<9.1f}{pf:<8.2f}")

    print("-" * 76)


if __name__ == "__main__":
    main()
