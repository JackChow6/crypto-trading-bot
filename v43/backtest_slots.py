#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_slots.py —— 带仓位上限约束的回测（考虑机会成本）。

关键修正：之前回测逐笔独立计算，忽略了「持仓上限 10 个」导致
持仓久的单占着名额、新机会被拒的问题。本回测按时间顺序重放，
维护持仓数，超过 max_open_positions 的信号被拒（= 错过的机会）。

对比：
  A. 固定止盈（无时间衰减）：持仓久 → 名额被占 → 新机会被拒
  B. 时间衰减止盈：持仓久且未盈利 → 尽早离场 → 释放名额 → 接住新机会
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
MAX_SLOTS = 10  # 与 config risk.max_open_positions 一致


def simulate_until_exit(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                        roi_mid_usd=None, roi_min_usd=None, mid_min=60, min_min=120):
    """返回 (pnl, exit_index)。exit_index 是平仓发生在第几根 1m bar（用于释放仓位名额的时间点）。

    若 roi_mid/min 为 None → 固定止盈；否则 → 时间衰减止盈。
    """
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / NOTIONAL)

    if side == "long":
        sl = entry - sl_dist
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            if roi_mid_usd is not None:
                # 时间衰减止盈门槛
                if minutes <= mid_min:
                    roi = tp_usd
                elif minutes <= min_min:
                    roi = roi_mid_usd
                else:
                    roi = roi_min_usd
                tp = entry + entry * (roi / NOTIONAL)
            else:
                tp = entry + entry * (tp_usd / NOTIONAL)
            if l <= sl:
                return bo.pnl_of(side, entry, sl), i
            if h >= tp:
                return bo.pnl_of(side, entry, tp), i
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        sl = entry + sl_dist
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            if roi_mid_usd is not None:
                if minutes <= mid_min:
                    roi = tp_usd
                elif minutes <= min_min:
                    roi = roi_mid_usd
                else:
                    roi = roi_min_usd
                tp = entry - entry * (roi / NOTIONAL)
            else:
                tp = entry - entry * (tp_usd / NOTIONAL)
            if h >= sl:
                return bo.pnl_of(side, entry, sl), i
            if l <= tp:
                return bo.pnl_of(side, entry, tp), i
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def run_backtest(prepared, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                 roi_mid_usd=None, roi_min_usd=None, mid_min=60, min_min=120):
    """按开仓时间顺序重放，维护持仓数，超上限的信号被拒。"""
    # prepared 已经按 trades 顺序（= 开仓时间顺序），但需确认有 opened_at
    # 重新从 trades 拿 opened_at 排序
    return simulate_slots(prepared, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                          roi_mid_usd, roi_min_usd, mid_min, min_min)


def simulate_slots(prepared, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                   roi_mid_usd, roi_min_usd, mid_min, min_min):
    """核心：按时间顺序模拟仓位占用/释放。"""
    # 每个 prepared 项需要 opened_at 时间戳（用于排序和计算持仓时长）
    # 但 prepare_trades 没存 opened_at，这里从 bars 反推：第一根 bar 的 ts 即开仓后
    items = []
    for p in prepared:
        if p["bars"]:
            open_ts = p["bars"][0][0]  # 开仓后第一根 bar 时间戳
        else:
            open_ts = 0
        items.append({"prep": p, "open_ts": open_ts})
    items.sort(key=lambda x: x["open_ts"])

    slots = []  # 当前持仓：[(exit_ts, pnl)]，exit_ts 为 None 表示还在持仓
    total_pnl = 0.0
    rejected = 0
    wins = 0
    closed_n = 0

    for item in items:
        p = item["prep"]
        open_ts = item["open_ts"]
        # 释放已平仓的仓位：如果某个持仓的 exit_ts <= 当前开仓时间，释放
        # 但我们需要知道每笔的 exit 时间点，所以先占位
        pass

    # 简化实现：直接顺序重放，用「持仓数」计数器，但需要 exit 时间点
    # 用 bars 长度估计持仓时长（分钟），exit 时间 ≈ open_ts + exit_index * 60000
    slots_list = []  # [(release_ts, pnl)]
    for item in items:
        p = item["prep"]
        open_ts = item["open_ts"]
        # 释放到期的仓位
        active = [s for s in slots_list if s[0] > open_ts]
        slots_list = active
        if len(slots_list) >= MAX_SLOTS:
            rejected += 1
            continue
        pnl, exit_idx = simulate_until_exit(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                            roi_mid_usd, roi_min_usd, mid_min, min_min)
        release_ts = open_ts + (exit_idx + 1) * 60_000
        slots_list.append((release_ts, pnl))
        total_pnl += pnl
        closed_n += 1
        if pnl > 0:
            wins += 1

    return total_pnl, wins, closed_n, rejected


def stats_from(total, wins, closed_n, rejected):
    wr = wins / closed_n * 100 if closed_n else 0
    return total, wr, rejected


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
    print(f"带仓位约束回测（上限 {MAX_SLOTS} 个，{len(prepared)} 个信号按时间重放）\n")

    print(f"{'方案':<36}{'总盈亏':<12}{'胜率':<10}{'被拒信号'}")
    print("-" * 72)

    # A. 固定止盈
    t, w, n, rej = run_backtest(prepared, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max)
    print(f"{'固定止盈':<36}{t:<+12.2f}{w:<10.1f}{rej}")

    # B. 时间衰减止盈（几组参数）
    combos = [
        ("衰减:1h降1u,2h保本0.5u", 1.0, 0.5, 60, 120),
        ("衰减:1h降1.5u,3h保本0.5u", 1.5, 0.5, 60, 180),
        ("衰减:2h降1u,4h保本0.5u", 1.0, 0.5, 120, 240),
        ("衰减:1h降1u,2h保本0.3u", 1.0, 0.3, 60, 120),
        ("衰减:0.5h降1.5u,1h保本0.5u", 1.5, 0.5, 30, 60),
    ]
    for label, mid, minu, mid_min, min_min in combos:
        t, w, n, rej = run_backtest(prepared, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                    mid, minu, mid_min, min_min)
        print(f"{label:<36}{t:<+12.2f}{w:<10.1f}{rej}")

    print("-" * 72)
    print("说明：'被拒信号'= 因持仓满而错过的新机会数，是时间衰减止盈要解决的核心问题")


if __name__ == "__main__":
    main()
