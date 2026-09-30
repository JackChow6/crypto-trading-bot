#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：1h ATR 动态止损是否真的在浮动，还是退化成了固定上限。"""
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
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 2.04))
    sl_min = float(e.get("sl_min_usd", 1.11))
    sl_max = float(e.get("sl_max_usd", 5.08))
    NOTIONAL = 100.0

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

    # 统计每笔交易的 ATR 止损金额（clamp 前原始值 + clamp 后）
    raw_sl = []      # clamp 前的 sl_usd = atr × mult × notional / entry
    hit_min = 0
    hit_max = 0
    in_range = 0
    no_atr = 0

    for t in trades:
        sym, entry = t["symbol"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        pre = [b for b in tf_bars.get(sym, []) if b[0] < opened_ms][-15:]
        atr = bo.atr_from_bars(pre)
        if not atr or not entry:
            no_atr += 1
            continue
        raw = atr * sl_atr_mult * NOTIONAL / entry
        raw_sl.append(raw)
        if raw <= sl_min:
            hit_min += 1
        elif raw >= sl_max:
            hit_max += 1
        else:
            in_range += 1

    n = len(raw_sl)
    print(f"诊断：1h ATR × {sl_atr_mult} 动态止损（clamp [{sl_min}, {sl_max}]u）\n")
    print(f"有效样本 {n} 笔（ATR 缺失 {no_atr} 笔）\n")
    print(f"{'分布':<22}{'笔数':<8}{'占比'}")
    print("-" * 40)
    print(f"{'撞下限(≤1.11u)':<20}{hit_min:<8}{hit_min/n*100:.1f}%")
    print(f"{'区间内(1.11~5.08u)':<20}{in_range:<8}{in_range/n*100:.1f}%")
    print(f"{'撞上限(≥5.08u)':<20}{hit_max:<8}{hit_max/n*100:.1f}%")
    print("-" * 40)

    if raw_sl:
        raw_sl.sort()
        print(f"\n原始止损金额(clamp前)分布：")
        print(f"  最小 {raw_sl[0]:.2f}u / 中位数 {raw_sl[n//2]:.2f}u / 最大 {raw_sl[-1]:.2f}u")
        print(f"  平均 {sum(raw_sl)/n:.2f}u")
        # 分位数
        def q(p): return raw_sl[int(n*p)]
        print(f"  25%分位 {q(0.25):.2f}u / 50% {q(0.5):.2f}u / 75% {q(0.75):.2f}u / 90% {q(0.9):.2f}u")

    print(f"\n结论：")
    if hit_max / n > 0.5:
        print(f"  ⚠️ {hit_max/n*100:.0f}% 的仓位止损金额撞到上限 5.08u —— 动态止损基本退化成固定 5.08u")
        print(f"  建议：降低 sl_atr_mult（如 {sl_atr_mult:.2f} → 1.2 左右），或提高 sl_max_usd，让止损真正浮动")
    elif hit_min / n > 0.5:
        print(f"  ⚠️ {hit_min/n*100:.0f}% 撞下限 —— 止损太紧")
    else:
        print(f"  ✅ 止损金额分布合理，动态止损真正在浮动")


if __name__ == "__main__":
    main()
