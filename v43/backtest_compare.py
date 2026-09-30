#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测指定止盈/止损组合的胜率和盈利，对比当前配置。"""
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
    paper = e.get("paper", {})
    sl_tf = e.get("sl_timeframe", "1h")
    split_ratio = float(paper.get("entry_split_ratio", 0.5))

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

    # 预处理：每笔的 entry/atr/bars（用 maker 入场均价）
    prepared = []
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in bars1m.get(sym, []) if ts >= opened_ms]
        pre = [b for b in tf_bars.get(sym, []) if b[0] < opened_ms][-15:]
        atr = bo.atr_from_bars(pre)
        # maker 入场均价
        if len(after) >= 2:
            avg = after[0][1] * split_ratio + after[1][4] * (1 - split_ratio)
        else:
            avg = entry
        prepared.append({"side": side, "entry": avg, "atr": atr, "bars": after})

    # 测试组合：动态止损固定倍数基准，只变止盈/止损金额
    def run(tp_usd, sl_base, sl_atr_mult, sl_min, sl_max, trail1, trail2, trail2_after, be_ratio):
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
        pf = sum(wl) / abs(sum(ll)) if ll else float("inf")
        return total, wins / n * 100, pf, sum(wl)/len(wl) if wl else 0, sum(ll)/len(ll) if ll else 0

    print(f"回测 {len(prepared)} 笔（maker入场 + {sl_tf} ATR 止损口径）\n")
    print(f"{'方案':<30}{'总盈亏':<10}{'胜率':<9}{'盈亏比':<8}{'均盈/均亏'}")
    print("-" * 70)

    # 当前配置
    cur = run(2.21, 2.49, 2.04, 1.11, 5.08, 3.84, 0.93, 7.45, 0.72)
    print(f"{'当前(2.21/2.49动态)':<28}{cur[0]:<+10.2f}{cur[1]:<9.1f}{cur[2]:<8.2f}{cur[3]:+.2f}/{cur[4]:.2f}")

    # 测试：止盈1.5u 止损3u，固定倍数动态止损
    t1 = run(1.5, 3.0, 2.04, 1.0, 4.0, 2.0, 0.5, 3.0, 0.5)
    print(f"{'1.5u止盈/3u止损(动态)':<28}{t1[0]:<+10.2f}{t1[1]:<9.1f}{t1[2]:<8.2f}{t1[3]:+.2f}/{t1[4]:.2f}")

    # 测试：止盈1.5u 止损3u，固定止损（无动态，sl_atr_mult=0 即用固定金额）
    t2 = run(1.5, 3.0, 0.0, 3.0, 3.0, 2.0, 0.5, 3.0, 0.5)
    print(f"{'1.5u止盈/3u止损(固定)':<28}{t2[0]:<+10.2f}{t2[1]:<9.1f}{t2[2]:<8.2f}{t2[3]:+.2f}/{t2[4]:.2f}")

    # 参考：止盈1.5u 止损1.5u
    t3 = run(1.5, 1.5, 0.0, 1.5, 1.5, 1.5, 0.5, 3.0, 0.5)
    print(f"{'1.5u止盈/1.5u止损(固定)':<28}{t3[0]:<+10.2f}{t3[1]:<9.1f}{t3[2]:<8.2f}{t3[3]:+.2f}/{t3[4]:.2f}")

    # 参考：止盈2.5u 止损4u
    t4 = run(2.5, 4.0, 0.0, 4.0, 4.0, 2.0, 0.5, 3.0, 0.5)
    print(f"{'2.5u止盈/4u止损(固定)':<28}{t4[0]:<+10.2f}{t4[1]:<9.1f}{t4[2]:<8.2f}{t4[3]:+.2f}/{t4[4]:.2f}")
    print("-" * 70)


if __name__ == "__main__":
    main()
