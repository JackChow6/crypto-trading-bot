#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比新旧止损参数下的持仓时长（bar 数）。"""
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


def simulate_held(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max, trail1, trail2, trail2_after, be_ratio):
    """返回 (pnl, bars_held)。"""
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * 100.0 / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / 100.0)
    tp_dist = entry * (tp_usd / 100.0)
    be = sl_dist * be_ratio
    trail1_d = entry * (trail1 / 100.0)
    trail2_d = entry * (trail2 / 100.0)
    trail2_after_d = entry * (trail2_after / 100.0)

    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and h >= tp:
                sl = max(sl, tp); tp = None
            eff = trail2_d if (trail2_after_d and highest >= entry + trail2_after_d) else trail1_d
            highest = max(highest, h)
            sl = max(sl, highest - eff)
            if highest >= entry + be:
                sl = max(sl, entry)
            if l <= sl:
                return pnl_of(side, entry, sl), i + 1
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and l <= tp:
                sl = min(sl, tp); tp = None
            eff = trail2_d if (trail2_after_d and lowest <= entry - trail2_after_d) else trail1_d
            lowest = min(lowest, l)
            sl = min(sl, lowest + eff)
            if lowest <= entry - be:
                sl = min(sl, entry)
            if h >= sl:
                return pnl_of(side, entry, sl), i + 1
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def pnl_of(side, entry, exit_px):
    gross = (exit_px - entry) / entry * 100.0 if side == "long" else (entry - exit_px) / entry * 100.0
    return gross - 100.0 * 0.05 / 100 * 2


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    sl_tf = e.get("sl_timeframe", "1h")
    split_ratio = float(e.get("paper", {}).get("entry_split_ratio", 0.5))

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

    # 旧参数（sl_atr_mult=2.04, 区间1.11~5.08）
    old = []
    for p in prepared:
        _, held = simulate_held(p, 2.21, 2.49, 2.04, 1.11, 5.08, 3.84, 0.93, 7.45, 0.72)
        old.append(held)
    # 新参数（sl_atr_mult=1.02, 区间1.71~4.82）
    new = []
    for p in prepared:
        _, held = simulate_held(p, 2.04, 2.53, 1.02, 1.71, 4.82, 2.83, 0.61, 7.84, 0.53)
        new.append(held)

    print(f"持仓时长对比（1m K 线根数，{len(prepared)} 笔）：\n")
    print(f"{'指标':<16}{'旧(倍2.04)':<14}{'新(倍1.02)':<14}")
    print("-" * 44)
    print(f"{'平均持仓':<16}{sum(old)/len(old):<14.1f}{sum(new)/len(new):<14.1f}")
    print(f"{'中位数':<16}{sorted(old)[len(old)//2]:<14.1f}{sorted(new)[len(new)//2]:<14.1f}")
    print(f"{'最短':<16}{min(old):<14}{min(new):<14}")
    print(f"{'最长':<16}{max(old):<14}{max(new):<14}")
    print("-" * 44)
    print(f"\n结论：新参数（止损更紧）持仓 {'变长' if sum(new)/len(new) > sum(old)/len(old) else '变短'}")


if __name__ == "__main__":
    main()
