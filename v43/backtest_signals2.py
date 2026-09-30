#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_signals2.py —— 今天上午全部信号 + 仓位约束 + 真实K线的完整回测。

对每个 decision 信号（含 ts/symbol/action/entry/sl/tp），按时间重放：
  - 拉该 symbol 信号时间之后的真实 1m K 线
  - 模拟固定止盈/止损 vs 时间衰减止盈
  - 维护仓位上限（10个），超限的信号被拒
对比两种止盈策略在「含机会成本」下的总盈亏。
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


def pnl_of(side, entry, exit_px, notional=NOTIONAL):
    if side == "long":
        gross = (exit_px - entry) / entry * notional
    else:
        gross = (entry - exit_px) / entry * notional
    return gross - notional * FEE_PCT / 100 * 2


def simulate_bars(side, entry, sl, tp, bars, time_roi=None):
    """模拟持仓到平仓。bars 是 [(ts,o,h,l,c),...]（信号时间之后）。
    time_roi: 时间衰减止盈门槛函数 (minutes) -> tp_usd 或 None。
    返回 (pnl, exit_idx)。
    """
    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            # 时间衰减：动态调 tp
            cur_tp = tp
            if time_roi is not None:
                roi = time_roi(minutes)
                if roi is not None:
                    cur_tp = entry + entry * (roi / NOTIONAL)
            if l <= sl:
                return pnl_of(side, entry, sl), i
            if h >= cur_tp:
                return pnl_of(side, entry, cur_tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            cur_tp = tp
            if time_roi is not None:
                roi = time_roi(minutes)
                if roi is not None:
                    cur_tp = entry - entry * (roi / NOTIONAL)
            if h >= sl:
                return pnl_of(side, entry, sl), i
            if l <= cur_tp:
                return pnl_of(side, entry, cur_tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def fetch_bars_after(ex, sym, ts_ms, limit=240):
    """拉信号时间之后的 1m K 线。"""
    try:
        bars = ex.fetch_ohlcv(sym, "1m", since=ts_ms - 60_000, limit=limit)
        return [(b[0], b[1], b[2], b[3], b[4]) for b in bars if b[0] >= ts_ms]
    except Exception:
        return []


def run_signals(ex, decs, bars_cache, time_roi=None):
    """按时间重放信号，维护仓位上限。返回 (总盈亏, 成交数, 被拒数, 盈利数)。"""
    slots = []  # [(release_ts, pnl)]
    total = 0.0
    wins = 0
    closed_n = 0
    rejected = 0

    for d in decs:
        sym = d.get("symbol")
        action = d.get("action")
        entry = d.get("entry", 0)
        sl = d.get("sl", 0)
        tp = d.get("tp", 0)
        ts = d.get("ts", 0)
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        side = action.lower()
        ts_ms = int(ts * 1000)

        # 释放到期的仓位
        slots = [s for s in slots if s[0] > ts_ms]

        if len(slots) >= MAX_SLOTS:
            rejected += 1
            continue

        # 拉真实 K 线
        bars = bars_cache.get((sym, ts_ms))
        if bars is None:
            bars = fetch_bars_after(ex, sym, ts_ms)
            bars_cache[(sym, ts_ms)] = bars
        if not bars:
            continue

        pnl, exit_idx = simulate_bars(side, entry, sl, tp, bars, time_roi)
        release_ts = ts_ms + (exit_idx + 1) * 60_000
        slots.append((release_ts, pnl))
        total += pnl
        closed_n += 1
        if pnl > 0:
            wins += 1

    return total, wins, closed_n, rejected


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)

    decs = load_decisions()
    now = time.time()
    today_start = now - (now % 86400)
    morning = [d for d in decs if d.get("ts", 0) >= today_start]
    print(f"今天上午 {len(morning)} 个信号，按时间重放（仓位上限 {MAX_SLOTS}）\n")

    # 从当前 config 拿止盈止损金额
    e = cfg["engine"]
    tp_usd = float(e.get("fixed_tp_usd", 2.19))
    sl_usd_base = float(e.get("fixed_sl_usd", 4.29))

    bars_cache = {}

    print(f"{'方案':<40}{'总盈亏':<12}{'胜率':<10}{'被拒'}")
    print("-" * 72)

    # A. 固定止盈（用 signal 自带的 sl/tp）
    t, w, n, rej = run_signals(ex, morning, bars_cache, time_roi=None)
    print(f"{'固定止盈(信号自带sl/tp)':<38}{t:<+12.2f}{w/n*100 if n else 0:<10.1f}{rej}")

    # B. 时间衰减止盈（几组）
    def make_roi(mid_usd, min_usd, mid_min, min_min):
        def roi(minutes):
            if minutes <= mid_min:
                return None  # 用信号自带 tp
            elif minutes <= min_min:
                return mid_usd
            else:
                return min_usd
        return roi

    combos = [
        ("衰减:1h降1u,2h保本0.5u", 1.0, 0.5, 60, 120),
        ("衰减:1h降1.5u,3h保本0.5u", 1.5, 0.5, 60, 180),
        ("衰减:2h降1u,4h保本0.5u", 1.0, 0.5, 120, 240),
        ("衰减:0.5h降1.5u,1h保本0.5u", 1.5, 0.5, 30, 60),
        ("衰减:1h降1u,2h保本0.3u", 1.0, 0.3, 60, 120),
    ]
    for label, mid, minu, mid_min, min_min in combos:
        roi_fn = make_roi(mid, minu, mid_min, min_min)
        t, w, n, rej = run_signals(ex, morning, bars_cache, time_roi=roi_fn)
        print(f"{label:<38}{t:<+12.2f}{w/n*100 if n else 0:<10.1f}{rej}")

    print("-" * 72)


if __name__ == "__main__":
    main()
