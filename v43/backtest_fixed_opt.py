#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_fixed_opt.py —— 当前策略（固定止盈/止损）最优参数网格扫描

对 live_state.json 里的全部真实成交（当前规则信号 + 动量选币产生的信号），
用币安 1m K 线重放每笔开仓后的价格路径，扫描固定止盈金额 tp_usd × 止损金额 sl_usd
组合，按总净盈亏排序输出最优。

固定止盈止损逻辑与引擎 fixed_mode 完全一致：保守先判止损、再判止盈。
名义金额 NOTIONAL = 100（5U 保证金 × 20 杠杆）。

用法：python v43/backtest_fixed_opt.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import backtest_scan as bs

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

NOTIONAL = 100.0
FEE_PCT = 0.05


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def simulate_fixed(side, entry, bars, tp_usd, sl_usd):
    """固定止盈止损：逐根 1m K线判断，保守先判止损再判止盈。返回 (exit_px, reason, held_bars)。"""
    tp_dist = entry * tp_usd / NOTIONAL
    sl_dist = entry * sl_usd / NOTIONAL
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
    for i, (ts, o, h, l, c) in enumerate(bars):
        if side == "long":
            if l <= sl:
                return sl, "SL", i + 1
            if h >= tp:
                return tp, "TP", i + 1
        else:
            if h >= sl:
                return sl, "SL", i + 1
            if l <= tp:
                return tp, "TP", i + 1
    return (bars[-1][4] if bars else entry), "OPEN", len(bars)


def evaluate(trades, bars_cache, tp_usd, sl_usd):
    pnls = []
    wins = 0
    tp_n = sl_n = open_n = skip_n = 0
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        allbars = bars_cache.get(sym, [])
        bars = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
        if not bars:
            skip_n += 1
            continue
        exit_px, reason, held = simulate_fixed(side, entry, bars, tp_usd, sl_usd)
        pnl = pnl_of(side, entry, exit_px)
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        if reason == "TP":
            tp_n += 1
        elif reason == "SL":
            sl_n += 1
        else:
            open_n += 1
    n = len(pnls)
    total = sum(pnls)
    wins_list = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    # 最大连续亏损
    cur = 0.0
    max_dd = 0.0
    for p in pnls:
        cur = cur + p if p < 0 else 0.0
        max_dd = min(max_dd, cur)
    return {
        "tp": tp_usd, "sl": sl_usd,
        "total": round(total, 2), "n": n, "win_rate": round(wins / n * 100, 1) if n else 0,
        "pf": round(sum(wins_list) / abs(sum(losses)), 2) if losses else float("inf"),
        "expect": round(total / n, 3) if n else 0,
        "max_dd": round(max_dd, 2),
        "tp_n": tp_n, "sl_n": sl_n, "open_n": open_n, "skip": skip_n,
    }


def main():
    trades = bs.load_trades()
    print(f"共 {len(trades)} 笔成交")

    cfg = __import__("yaml").safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    bars_cache = bs.get_bars_cache(ex, trades)
    print(f"K 线缓存：{len(bars_cache)} 币种\n")

    # 网格：止盈 × 止损（覆盖当前 2.0u / 0.7u）
    tp_grid = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    sl_grid = [0.3, 0.5, 0.7, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

    results = []
    for tp in tp_grid:
        for sl in sl_grid:
            results.append(evaluate(trades, bars_cache, tp, sl))

    results.sort(key=lambda r: r["total"], reverse=True)

    print("=" * 96)
    print(f"{'排名':<4}{'止盈':<6}{'止损':<6}{'总盈亏':<9}{'胜率':<8}{'盈亏比':<7}{'期望/笔':<9}{'最大连亏':<9}{'TP/SL/未平/跳'}")
    print("=" * 96)
    for i, r in enumerate(results[:30]):
        print(f"{i+1:<4}{r['tp']:<6}{r['sl']:<6}{r['total']:<+9}{r['win_rate']:<8}{r['pf']:<7}"
              f"{r['expect']:<+9}{r['max_dd']:<+9}{r['tp_n']}/{r['sl_n']}/{r['open_n']}/{r['skip']}")

    # 当前配置排名
    cur = next((r for r in results if abs(r["tp"] - 2.0) < 1e-9 and abs(r["sl"] - 0.7) < 1e-9), None)
    if cur:
        rank = results.index(cur) + 1
        print(f"\n当前配置（固定 2.0u/0.7u）：第 {rank} / {len(results)} 名，总盈亏 {cur['total']:+.2f}，胜率 {cur['win_rate']}%")

    best = results[0]
    print("\n" + "=" * 96)
    print(f"全局最优：止盈 {best['tp']}u / 止损 {best['sl']}u")
    print(f"  总盈亏 {best['total']:+.2f} | 胜率 {best['win_rate']}% | 盈亏比 {best['pf']} | "
          f"期望 {best['expect']:+.3f}/笔 | 最大连亏 {best['max_dd']} | TP {best['tp_n']} / SL {best['sl_n']} / 未平 {best['open_n']}")
    print("=" * 96)
    print("注意：样本为历史信号，存在过拟合风险，最优参数建议小资金实盘验证后再切换。")

    # 输出 JSON 供后续参考
    out = os.path.join(BASE, "backtest_fixed_opt_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results[:30], f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 {out}")


if __name__ == "__main__":
    main()
