#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_st.py —— 回测 Supertrend 止损 vs ATR 金额止损。

用实盘成交 + 真实 K 线，对比：
  A. Supertrend 止损（做多下轨/做空上轨，随趋势单向移动）+ 固定锁利
  B. ATR 金额动态止损（旧方法）+ 固定锁利
其余止盈逻辑一致。
"""
from __future__ import annotations

import json
import os
import sys
import time

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import backtest_scan as bs
import features

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")

NOTIONAL = 100.0
FEE_PCT = 0.05


def load_trades():
    with open(STATE, "r", encoding="utf-8") as f:
        return json.load(f).get("trades", [])


def load_bars():
    with open(CACHE, "r", encoding="utf-8") as f:
        return json.load(f).get("bars", {})


def simulate_st(side, entry, bars, tp_usd, st_period, st_mult):
    """Supertrend 止损 + 固定锁利，返回净盈亏。"""
    tp_dist = entry * (tp_usd / NOTIONAL)
    if side == "long":
        tp = entry + tp_dist
        sl = None
        highest = entry
        # 用开仓前+后 K 线序列逐根更新 supertrend 止损价
        for i in range(len(bars)):
            ts, o, h, l, c = bars[i]
            highest = max(highest, h)
            # 锁利
            if tp is not None and h >= tp:
                sl = max(sl or 0, tp)
                tp = None
            # supertrend 止损：用截至当前根的历史窗口算轨线
            window = bars[max(0, i - 119):i + 1]
            if len(window) >= st_period + 1:
                _, st_line, upper, lower = features.supertrend(window, st_period, st_mult)
                if st_line is not None:
                    band = lower if lower is not None else st_line
                    sl = max(sl or 0, band)
            if sl is not None and l <= sl:
                return pnl_of(side, entry, sl)
        return pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        tp = entry - tp_dist
        sl = None
        lowest = entry
        for i in range(len(bars)):
            ts, o, h, l, c = bars[i]
            lowest = min(lowest, l)
            if tp is not None and l <= tp:
                sl = min(sl if sl is not None else float("inf"), tp)
                tp = None
            window = bars[max(0, i - 119):i + 1]
            if len(window) >= st_period + 1:
                _, st_line, upper, lower = features.supertrend(window, st_period, st_mult)
                if st_line is not None:
                    band = upper if upper is not None else st_line
                    sl = min(sl if sl is not None else float("inf"), band)
            if sl is not None and h >= sl:
                return pnl_of(side, entry, sl)
        return pnl_of(side, entry, bars[-1][4] if bars else entry)


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    tp_usd = float(e.get("fixed_tp_usd", 2.21))
    st_period = int(e.get("st_period", 10))
    st_mult = float(e.get("st_mult", 3.0))

    trades = load_trades()
    bars_cache = load_bars()

    print(f"回测 Supertrend 止损（period={st_period}, mult={st_mult}, 锁利{tp_usd}u）\n")

    # 为每个参数组合跑
    grid = [(10, 2.0), (10, 3.0), (10, 4.0), (14, 3.0), (7, 3.0), (20, 3.0)]
    print(f"{'st_period':<10}{'st_mult':<10}{'总盈亏':<12}{'胜率':<10}{'盈亏比':<10}")
    print("-" * 52)
    best = None
    for period, mult in grid:
        pnls = []
        wins = 0
        for t in trades:
            sym, side, entry = t["symbol"], t["side"], t["entry"]
            opened_ms = int(t["opened_at"] * 1000)
            allbars = bars_cache.get(sym, [])
            pre = [b for b in allbars if b[0] < opened_ms][-120:]
            after = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
            bars = pre + after   # 完整序列，supertrend 需要历史窗口
            pnl = simulate_st(side, entry, bars, tp_usd, period, mult)
            pnls.append(pnl)
            if pnl > 0:
                wins += 1
        total = sum(pnls)
        wr = wins / len(pnls) * 100
        wl = [x for x in pnls if x > 0]
        ll = [x for x in pnls if x <= 0]
        pf = (sum(wl) / abs(sum(ll))) if ll else float("inf")
        print(f"{period:<10}{mult:<10}{total:<+12.2f}{wr:<10.1f}{pf:<10.2f}")
        if best is None or total > best[2]:
            best = (period, mult, total, wr, pf)

    print("-" * 52)
    print(f"最优：st_period={best[0]}, st_mult={best[1]}, 总盈亏{best[2]:+.2f}, 胜率{best[3]:.1f}%, 盈亏比{best[4]:.2f}")


if __name__ == "__main__":
    main()
