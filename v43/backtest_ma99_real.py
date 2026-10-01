#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_ma99_real.py —— 用真实成交验证「4h MA99 定方向」规则

数据源：live_state.json 的 863 笔真实成交（实盘动量榜 + 规则信号真实产生）。
对每笔成交，拉成交时点(opened_at)之前的 4h K线算 MA99 趋势方向，
统计「实盘实际方向 vs 4hMA99 方向」一致/矛盾时，实际盈亏分布。

关键对比：
  1. 实际方向 = MA99 方向（一致）：这些单赚还是亏？
  2. 实际方向 ≠ MA99 方向（矛盾）：这些单赚还是亏？
  → 如果「一致时赚、矛盾时亏」，说明 MA99 定方向有效；
  → 如果相反，说明 MA99 定方向无效（甚至该反向）。

用法：python v43/backtest_ma99_real.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import ccxt
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

STATE = os.path.join(BASE, "live_state.json")
CONFIG = os.path.join(ROOT, "config.yaml")

MA99_PERIOD = 99


def build_exchange(cfg):
    xc = cfg["exchange"]
    params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if xc.get("proxy"):
        params["proxies"] = {"http": xc["proxy"], "https": xc["proxy"]}
    ex = getattr(ccxt, xc["name"])(params)
    ex.load_markets()
    return ex


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)

    with open(STATE, "r", encoding="utf-8") as f:
        trades = json.load(f).get("trades", [])
    print(f"真实成交 {len(trades)} 笔\n")

    # 需要每个币种 4h K线。为省 REST，按币种去重，拉 27 天（99根4h需16.5天）
    syms = sorted({t["symbol"] for t in trades})
    print(f"[1] 拉 {len(syms)} 个币的 4h K线（27 天）...")
    bars4h = {}
    for sym in syms:
        try:
            o = ex.fetch_ohlcv(sym, "4h", limit=27 * 6 + 10)
            if o:
                bars4h[sym] = [(b[0], b[4]) for b in o]   # (ts, close)
        except Exception:
            pass
    print(f"    有效 4h 数据 {len(bars4h)} 币\n")

    # 对每笔成交判断 4h MA99 方向
    rows = []
    for t in trades:
        sym = t["symbol"]
        side = t["side"]            # 实盘实际方向
        pnl = t.get("pnl", 0.0)
        opened = t.get("opened_at", 0)
        c4 = bars4h.get(sym, [])
        if not c4 or not opened:
            continue
        before = [c for ts, c in c4 if ts <= opened * 1000]
        if len(before) < MA99_PERIOD:
            continue
        ma = sum(before[-MA99_PERIOD:]) / MA99_PERIOD
        ma_dir = "long" if before[-1] > ma else "short"
        agree = (side == ma_dir)
        rows.append({"sym": sym, "side": side, "ma": ma_dir, "agree": agree, "pnl": pnl})

    n = len(rows)
    print(f"[2] 有效配对 {n} 笔\n")

    agree_rows = [r for r in rows if r["agree"]]
    disagree_rows = [r for r in rows if not r["agree"]]

    def stat(rs, label):
        if not rs:
            print(f"  {label}: 无样本")
            return
        pnls = [r["pnl"] for r in rs]
        total = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls) * 100
        pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
        print(f"  {label:<24} n={len(rs):<4} 总盈亏{total:+9.2f} 胜率{wr:5.1f}% "
              f"盈亏比{pf:5.2f} 期望{total/len(rs):+.3f}/笔")

    print("[3] 实盘实际方向 vs 4hMA99 方向：")
    print("-" * 80)
    stat(agree_rows, "一致（方向=MA99）")
    stat(disagree_rows, "矛盾（方向≠MA99）")

    # 分方向明细
    print("\n[4] 按 实际方向×MA99 四象限：")
    print("-" * 80)
    for side in ("long", "short"):
        for ma in ("long", "short"):
            rs = [r for r in rows if r["side"] == side and r["ma"] == ma]
            agree = "一致" if side == ma else "矛盾"
            stat(rs, f"{side}+MA99{ma}({agree})")

    # 关键结论：如果按 MA99 方向重做（矛盾单反转方向），总盈亏会变成多少？
    print("\n[5] 假设：矛盾单反转方向后（= 完全按 MA99 方向下单）的总盈亏：")
    print("-" * 80)
    # 反转矛盾单方向 → 盈亏近似取负（对称近似，忽略手续费方向的细微差）
    pnl_ma99 = sum(r["pnl"] if r["agree"] else -r["pnl"] for r in rows)
    pnl_actual = sum(r["pnl"] for r in rows)
    print(f"  实盘实际总盈亏        : {pnl_actual:+.2f}")
    print(f"  完全按MA99方向(假设)  : {pnl_ma99:+.2f}")
    print(f"  差异（MA99方向 减 实际）: {pnl_ma99 - pnl_actual:+.2f}")


if __name__ == "__main__":
    main()
