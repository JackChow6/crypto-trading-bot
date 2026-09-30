#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测锁利门槛对比：1.5u vs 2.04u vs 1.8u。"""
from __future__ import annotations
import json, os, sys, time
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")


def fetch_tf_bars(ex, sym, timeframe, since_ms, until_ms):
    out, cursor = [], since_ms
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(sym, timeframe, since=cursor, limit=1000)
        except Exception:
            break
        if not bars:
            break
        for b in bars:
            if since_ms <= b[0] <= until_ms:
                out.append([b[0], b[1], b[2], b[3], b[4]])
        last = bars[-1][0]
        if last <= cursor:
            break
        cursor = last + step
    return out


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    sl_tf = e.get("sl_timeframe", "1h")
    split_ratio = float(e.get("paper", {}).get("entry_split_ratio", 0.5))
    # 固定其余参数（当前值）
    sl_base = float(e.get("fixed_sl_usd", 2.53))
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 1.02))
    sl_min = float(e.get("sl_min_usd", 1.71))
    sl_max = float(e.get("sl_max_usd", 4.82))
    trail1 = float(e.get("trail_step1_usd", 2.83))
    trail2 = float(e.get("trail_step2_usd", 0.61))
    trail2_after = float(e.get("trail_step2_after_usd", 7.84))
    be_ratio = float(e.get("break_even_ratio", 0.53))

    trades = bo.load_trades()
    bars1m = bo.load_bars()
    ex = bs.build_exchange(cfg)
    syms = sorted({t["symbol"] for t in trades})
    tf_bars = {}
    for sym in syms:
        ts = [t for t in trades if t["symbol"] == sym]
        since = int(min(t["opened_at"] for t in ts) * 1000) - 7 * 24 * 3600_000
        until = int(time.time() * 1000)
        tf_bars[sym] = fetch_tf_bars(ex, sym, sl_tf, since, until)

    prepared = []
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in bars1m.get(sym, []) if ts >= opened_ms]
        pre = [b for b in tf_bars.get(sym, []) if b[0] < opened_ms][-15:]
        atr = bo.atr_from_bars(pre)
        avg = after[0][1] * split_ratio + after[1][4] * (1 - split_ratio) if len(after) >= 2 else entry
        prepared.append({"side": side, "entry": avg, "atr": atr, "bars": after})

    def run(tp_usd):
        pnls, wins = [], 0
        for p in prepared:
            pnl = bo.simulate(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                              trail1, trail2, trail2_after, be_ratio)
            pnls.append(pnl)
            if pnl > 0:
                wins += 1
        n = len(pnls)
        total = sum(pnls)
        wl = [x for x in pnls if x > 0]
        ll = [x for x in pnls if x <= 0]
        pf = sum(wl)/abs(sum(ll)) if ll else float("inf")
        return total, wins/n*100, pf

    print(f"锁利门槛对比（{len(prepared)} 笔，其余参数=当前值）\n")
    print(f"{'锁利门槛':<12}{'总盈亏':<12}{'胜率':<10}{'盈亏比':<10}")
    print("-" * 44)
    for tp in [1.2, 1.5, 1.8, 2.04, 2.3, 2.5]:
        total, wr, pf = run(tp)
        mark = " ← 当前" if abs(tp - 2.04) < 0.01 else ""
        print(f"{tp:<12}{total:<+12.2f}{wr:<10.1f}{pf:<10.2f}{mark}")
    print("-" * 44)


if __name__ == "__main__":
    main()
