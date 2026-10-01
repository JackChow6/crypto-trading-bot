#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_live_50x.py —— 用当前 50X 配置回测实盘真实成交记录

对 live_state.json 里的每一笔真实成交（实盘真实信号产生的开仓），
用币安 1m K线重放开仓后的价格路径，按「50X 杠杆（名义 250u）+ 固定止盈 5u / 止损 7.5u」
模拟平仓，统计总盈亏/胜率/盈亏比/最大连亏。

同时对比 20X 口径（名义 100u / 止盈 2u / 止损 3u）作为参照，
验证「价格距离百分比不变（2%/3%），只是金额放大 2.5 倍」是否成立。

用法：python v43/backtest_live_50x.py
"""
from __future__ import annotations

import os
import sys
import time

import backtest_scan as bs

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)


def pnl_of(side, entry, exit_px, notional):
    if side == "long":
        gross = (exit_px - entry) / entry * notional
    else:
        gross = (entry - exit_px) / entry * notional
    return gross - notional * bs.FEE_PCT / 100 * 2


def simulate_fixed(side, entry, bars, tp_usd, sl_usd, notional):
    tp_dist = entry * tp_usd / notional
    sl_dist = entry * sl_usd / notional
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        for ts, o, h, l, c in bars:
            if l <= sl:
                return pnl_of(side, entry, sl, notional), "SL"
            if h >= tp:
                return pnl_of(side, entry, tp, notional), "TP"
        return pnl_of(side, entry, bars[-1][4] if bars else entry, notional), "OPEN"
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        for ts, o, h, l, c in bars:
            if h >= sl:
                return pnl_of(side, entry, sl, notional), "SL"
            if l <= tp:
                return pnl_of(side, entry, tp, notional), "TP"
        return pnl_of(side, entry, bars[-1][4] if bars else entry, notional), "OPEN"


def run(trades, bars_cache, tp_usd, sl_usd, notional, label):
    pnls = []
    tp_n = sl_n = open_n = 0
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        allbars = bars_cache.get(sym, [])
        bars = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
        if not bars:
            continue
        p, reason = simulate_fixed(side, entry, bars, tp_usd, sl_usd, notional)
        pnls.append(p)
        if reason == "TP":
            tp_n += 1
        elif reason == "SL":
            sl_n += 1
        else:
            open_n += 1
    n = len(pnls)
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100 if n else 0
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    cur = 0.0
    max_dd = 0.0
    for p in pnls:
        cur = cur + p if p < 0 else 0.0
        max_dd = min(max_dd, cur)
    print(f"  {label:<18} n={n:<4} 总盈亏{total:+9.2f} 胜率{wr:5.1f}% 盈亏比{pf:5.2f} "
          f"期望{total/n:+.3f}/笔 最大连亏{max_dd:8.2f}  TP{tp_n}/SL{sl_n}/未平{open_n}")
    return total


def main():
    trades = bs.load_trades()
    print(f"实盘成交 {len(trades)} 笔\n")

    cfg = __import__("yaml").safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    bars_cache = bs.get_bars_cache(ex, trades)
    print(f"K线缓存：{len(bars_cache)} 币\n")

    print("[回测结果] 实盘真实成交，按固定止盈止损重放价格路径：")
    print("-" * 100)
    run(trades, bars_cache, 5.0, 7.5, 250.0, "50X(5u/7.5u)")
    run(trades, bars_cache, 2.0, 3.0, 100.0, "20X(2u/3u)")

    # 实盘实际已实现盈亏（真实记录，含手续费/滑点）
    actual = sum(t["pnl"] for t in trades)
    print(f"\n  实盘实际累计盈亏（记录值）: {actual:+.2f}")


if __name__ == "__main__":
    main()
