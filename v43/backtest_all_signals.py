#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_all_signals.py —— 让「所有信号」（含被拒的）都进场，算它们的真实盈亏。

回答核心问题：被拒的 67~69 个信号，如果进场了，是赚还是亏？
从而判断「时间衰减释放名额」到底值不值：
  - 被拒信号进场普遍赚钱 → 释放名额很有价值，时间衰减该更激进
  - 被拒信号进场普遍亏钱 → 仓位上限是保护，被拒是好事

对比：无仓位上限（所有信号都进场） vs 有仓位上限（当前）。
"""
from __future__ import annotations
import json, os, sys, time
import yaml
import ccxt

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
DECISION_LOG = os.path.join(BASE, "decision_log.jsonl")
NOTIONAL = 100.0
FEE_PCT = 0.05


def load_decisions():
    decs = []
    with open(DECISION_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except Exception:
                continue
            if d.get("type") == "decision":
                decs.append(d)
    decs.sort(key=lambda d: d.get("ts", 0))
    return decs


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def simulate(side, entry, bars, mode, params):
    """按 mode 模拟，返回 (pnl, exit_idx)。"""
    if mode == "D":
        return sim_dynamic(side, entry, bars, params)
    tp_usd = params["tp"]; sl_usd = params["sl"]
    roi_mid = params.get("roi_mid"); roi_min = params.get("roi_min")
    mid_min = params.get("mid_min", 60); min_min = params.get("min_min", 180)
    sl_decay_mid = params.get("sl_decay_mid"); sl_decay_min = params.get("sl_decay_min")
    sl_mid_min = params.get("sl_mid_min", 120); sl_min_min = params.get("sl_min_min", 240)

    def cur_sl(m):
        if sl_decay_mid is None:
            return sl_usd
        if m <= sl_mid_min: return sl_usd
        elif m <= sl_min_min: return sl_decay_mid
        return sl_decay_min

    def cur_tp(m):
        if roi_mid is None:
            return tp_usd
        if m <= mid_min: return tp_usd
        elif m <= min_min: return roi_mid
        return roi_min

    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry - entry * (cur_sl(m) / NOTIONAL)
            tp = entry + entry * (cur_tp(m) / NOTIONAL)
            if l <= sl: return pnl_of(side, entry, sl), i
            if h >= tp: return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry + entry * (cur_sl(m) / NOTIONAL)
            tp = entry - entry * (cur_tp(m) / NOTIONAL)
            if h >= sl: return pnl_of(side, entry, sl), i
            if l <= tp: return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def sim_dynamic(side, entry, bars, params):
    tp_usd = params["tp"]; sl_usd = params["sl"]
    trail1 = params["trail1"]; trail2 = params["trail2"]
    trail2_after = params["trail2_after"]; be_ratio = params["be_ratio"]
    sl_dist = entry * (sl_usd / NOTIONAL); tp_dist = entry * (tp_usd / NOTIONAL)
    be = sl_dist * be_ratio
    t1 = entry * (trail1 / NOTIONAL); t2 = entry * (trail2 / NOTIONAL); ta = entry * (trail2_after / NOTIONAL)
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and h >= tp:
                sl = max(sl, tp); tp = None
            eff = t2 if highest >= entry + ta else t1
            highest = max(highest, h); sl = max(sl, highest - eff)
            if highest >= entry + be: sl = max(sl, entry)
            if l <= sl: return pnl_of(side, entry, sl), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and l <= tp:
                sl = min(sl, tp); tp = None
            eff = t2 if lowest <= entry - ta else t1
            lowest = min(lowest, l); sl = min(sl, lowest + eff)
            if lowest <= entry - be: sl = min(sl, entry)
            if h >= sl: return pnl_of(side, entry, sl), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def fetch_bars_after(ex, sym, ts_ms, limit=600):
    try:
        bars = ex.fetch_ohlcv(sym, "1m", since=ts_ms - 60_000, limit=limit)
        return [(b[0], b[1], b[2], b[3], b[4]) for b in bars if b[0] >= ts_ms]
    except Exception:
        return []


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    decs = load_decisions()
    now = time.time()
    today_start = now - (now % 86400)
    morning = [d for d in decs if d.get("ts", 0) >= today_start]
    print(f"今天上午 {len(morning)} 个信号，全部进场（无仓位上限）回测\n")

    modes = {
        "A": {"tp": 2.19, "sl": 4.29},
        "B": {"tp": 2.19, "sl": 2.79, "roi_mid": 2.13, "roi_min": 0.57, "mid_min": 112, "min_min": 213},
        "C": {"tp": 2.20, "sl": 4.38, "roi_mid": 2.03, "roi_min": 0.12, "mid_min": 119, "min_min": 340,
              "sl_decay_mid": 1.05, "sl_decay_min": 0.27, "sl_mid_min": 176, "sl_min_min": 337},
        "D": {"tp": 2.04, "sl": 2.53, "trail1": 2.83, "trail2": 0.61, "trail2_after": 7.84, "be_ratio": 0.53},
    }
    labels = {
        "A": "A 纯固定止盈2.19u/止损4.29u",
        "B": "B 固定止盈+止盈衰减",
        "C": "C 固定止盈+止盈衰减+止损衰减(当前)",
        "D": "D 动态止盈(锁利继续跑)",
    }

    bars_cache = {}
    print(f"{'方案':<46}{'总盈亏':<12}{'胜率':<9}")
    print("-" * 70)
    all_pnls = {}
    for mode in ["A", "B", "C", "D"]:
        pnls = []
        for d in morning:
            sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry"); ts = d.get("ts", 0)
            if not sym or not entry or action not in ("LONG", "SHORT"):
                continue
            side = action.lower()
            ts_ms = int(ts * 1000)
            key = (sym, ts_ms)
            bars = bars_cache.get(key)
            if bars is None:
                bars = fetch_bars_after(ex, sym, ts_ms)
                bars_cache[key] = bars
            if not bars:
                continue
            pnl, _ = simulate(side, entry, bars, mode, modes[mode])
            pnls.append(pnl)
        all_pnls[mode] = pnls
        total = sum(pnls); wins = sum(1 for x in pnls if x > 0)
        wr = wins / len(pnls) * 100 if pnls else 0
        print(f"{labels[mode]:<44}{total:<+12.2f}{wr:<9.1f}")
    print("-" * 70)

    # 关键：被拒的信号（= 第 11 个之后的信号，因为上限 10）进场是赚是亏
    # 按时间排序，模拟"持仓上限 10"，找出被拒的信号，看它们的盈亏
    print("\n=== 被拒信号（若进场）的真实盈亏分析 ===")
    # 用 A 方案 + 仓位上限 10，找出哪些信号被拒
    slots = []
    accepted_idx = []
    rejected_idx = []
    for i, d in enumerate(morning):
        ts_ms = int(d.get("ts", 0) * 1000)
        slots = [s for s in slots if s[0] > ts_ms]
        if len(slots) >= 10:
            rejected_idx.append(i)
            continue
        accepted_idx.append(i)
        # 用 A 方案估算持仓时长
        sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry")
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        side = action.lower()
        bars = bars_cache.get((sym, ts_ms))
        if bars is None:
            bars = fetch_bars_after(ex, sym, ts_ms); bars_cache[(sym, ts_ms)] = bars
        if not bars:
            continue
        pnl, exit_idx = simulate(side, entry, bars, "A", modes["A"])
        slots.append((ts_ms + (exit_idx + 1) * 60_000, pnl))

    print(f"被拒信号数: {len(rejected_idx)}")
    # 这些被拒信号如果进场（用 A 方案），盈亏如何
    rejected_pnls = []
    for i in rejected_idx:
        d = morning[i]
        sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry")
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        side = action.lower()
        ts_ms = int(d.get("ts", 0) * 1000)
        bars = bars_cache.get((sym, ts_ms))
        if bars is None:
            bars = fetch_bars_after(ex, sym, ts_ms); bars_cache[(sym, ts_ms)] = bars
        if not bars:
            continue
        pnl, _ = simulate(side, entry, bars, "A", modes["A"])
        rejected_pnls.append(pnl)

    if rejected_pnls:
        total_rej = sum(rejected_pnls)
        wins_rej = sum(1 for x in rejected_pnls if x > 0)
        print(f"被拒信号若进场（A方案）：总盈亏 {total_rej:+.2f}，盈利 {wins_rej}/{len(rejected_pnls)}，胜率 {wins_rej/len(rejected_pnls)*100:.1f}%")


if __name__ == "__main__":
    main()
