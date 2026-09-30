#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_signals_compare.py —— 基于「产生的信号」对比四种止盈止损设置。

数据源：decision_log.jsonl 的所有信号（含被拒的，共 786 个，今天上午 100+ 个）。
每个信号按时间重放 + 仓位上限约束（10个），拉真实 K 线模拟四种设置：
  A. 纯固定止盈/止损
  B. 固定止盈 + 止盈时间衰减
  C. 固定止盈 + 止盈衰减 + 止损衰减
  D. 动态止盈（锁利继续跑）
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
MAX_SLOTS = 10


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
    """按 mode(A/B/C/D) 模拟，返回 (pnl, exit_idx)。"""
    if mode == "D":
        return sim_dynamic(side, entry, bars, params)
    # A/B/C 都是固定止盈，只是衰减参数不同
    tp_usd = params["tp"]
    sl_usd = params["sl"]
    roi_mid = params.get("roi_mid")
    roi_min = params.get("roi_min")
    mid_min = params.get("mid_min", 60)
    min_min = params.get("min_min", 180)
    sl_decay_mid = params.get("sl_decay_mid")
    sl_decay_min = params.get("sl_decay_min")
    sl_mid_min = params.get("sl_mid_min", 120)
    sl_min_min = params.get("sl_min_min", 240)

    def cur_sl(minutes):
        if sl_decay_mid is None:
            return sl_usd
        if minutes <= sl_mid_min:
            return sl_usd
        elif minutes <= sl_min_min:
            return sl_decay_mid
        return sl_decay_min

    def cur_tp(minutes):
        if roi_mid is None:
            return tp_usd
        if minutes <= mid_min:
            return tp_usd
        elif minutes <= min_min:
            return roi_mid
        return roi_min

    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry - entry * (cur_sl(m) / NOTIONAL)
            tp = entry + entry * (cur_tp(m) / NOTIONAL)
            if l <= sl:
                return pnl_of(side, entry, sl), i
            if h >= tp:
                return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry + entry * (cur_sl(m) / NOTIONAL)
            tp = entry - entry * (cur_tp(m) / NOTIONAL)
            if h >= sl:
                return pnl_of(side, entry, sl), i
            if l <= tp:
                return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def sim_dynamic(side, entry, bars, params):
    tp_usd = params["tp"]; sl_usd = params["sl"]
    trail1 = params["trail1"]; trail2 = params["trail2"]
    trail2_after = params["trail2_after"]; be_ratio = params["be_ratio"]
    sl_dist = entry * (sl_usd / NOTIONAL)
    tp_dist = entry * (tp_usd / NOTIONAL)
    be = sl_dist * be_ratio
    t1 = entry * (trail1 / NOTIONAL); t2 = entry * (trail2 / NOTIONAL); ta = entry * (trail2_after / NOTIONAL)
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and h >= tp:
                sl = max(sl, tp); tp = None
            eff = t2 if highest >= entry + ta else t1
            highest = max(highest, h)
            sl = max(sl, highest - eff)
            if highest >= entry + be:
                sl = max(sl, entry)
            if l <= sl:
                return pnl_of(side, entry, sl), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and l <= tp:
                sl = min(sl, tp); tp = None
            eff = t2 if lowest <= entry - ta else t1
            lowest = min(lowest, l)
            sl = min(sl, lowest + eff)
            if lowest <= entry - be:
                sl = min(sl, entry)
            if h >= sl:
                return pnl_of(side, entry, sl), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def fetch_bars_after(ex, sym, ts_ms, limit=600):
    try:
        bars = ex.fetch_ohlcv(sym, "1m", since=ts_ms - 60_000, limit=limit)
        return [(b[0], b[1], b[2], b[3], b[4]) for b in bars if b[0] >= ts_ms]
    except Exception:
        return []


def run(ex, decs, mode, params, bars_cache):
    slots = []  # (release_ts, pnl)
    total = 0.0; wins = 0; closed = 0; rejected = 0
    for d in decs:
        sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry"); ts = d.get("ts", 0)
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        side = action.lower()
        ts_ms = int(ts * 1000)
        slots = [s for s in slots if s[0] > ts_ms]
        if len(slots) >= MAX_SLOTS:
            rejected += 1
            continue
        key = (sym, ts_ms)
        bars = bars_cache.get(key)
        if bars is None:
            bars = fetch_bars_after(ex, sym, ts_ms)
            bars_cache[key] = bars
        if not bars:
            continue
        pnl, exit_idx = simulate(side, entry, bars, mode, params)
        release_ts = ts_ms + (exit_idx + 1) * 60_000
        slots.append((release_ts, pnl))
        total += pnl; closed += 1
        if pnl > 0:
            wins += 1
    return total, wins, closed, rejected


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    decs = load_decisions()
    now = time.time()
    today_start = now - (now % 86400)
    morning = [d for d in decs if d.get("ts", 0) >= today_start]
    print(f"基于产生信号的对比回测（今天上午 {len(morning)} 个信号，含被拒的，仓位上限 {MAX_SLOTS}）\n")

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
    print(f"{'方案':<46}{'总盈亏':<12}{'胜率':<9}{'被拒'}")
    print("-" * 74)
    results = {}
    for mode in ["A", "B", "C", "D"]:
        t, w, n, rej = run(ex, morning, mode, modes[mode], bars_cache)
        wr = w / n * 100 if n else 0
        results[mode] = t
        print(f"{labels[mode]:<44}{t:<+12.2f}{wr:<9.1f}{rej}")
    print("-" * 74)

    best = max(results, key=lambda k: results[k])
    print(f"\n最优：{labels[best]}（总盈亏 {results[best]:+.2f}）")


if __name__ == "__main__":
    main()
