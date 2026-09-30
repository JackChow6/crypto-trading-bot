#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_sl_tf.py —— 回测多时间框架止损：对比不同周期 ATR 做动态止损的效果。

分别用 15m / 1h / 4h 的 ATR 计算动态止损金额，其余止盈止损参数保持一致，
看哪个止损周期总盈亏最高（衡量"更高周期 ATR 能否避免被短周期噪音震出"）。
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

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")


def fetch_tf_bars(ex, sym, timeframe, since_ms, until_ms):
    """拉指定周期的 K 线（复用 bs.fetch_bars，但 fetch_bars 只支持固定 60s 步进，这里直接写）。"""
    out = []
    cursor = since_ms
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(sym, timeframe, since=cursor, limit=1000)
        except Exception as e:
            print(f"    拉取 {sym} {timeframe} 失败: {e}")
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

    # 当前寻优参数
    tp_usd = float(e.get("fixed_tp_usd", 2.55))
    sl_base = float(e.get("fixed_sl_usd", 1.1))
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 3.72))
    sl_min = float(e.get("sl_min_usd", 1.46))
    sl_max = float(e.get("sl_max_usd", 6.61))
    trail1 = float(e.get("trail_step1_usd", 2.78))
    trail2 = float(e.get("trail_step2_usd", 0.44))
    trail2_after = float(e.get("trail_step2_after_usd", 4.98))
    be_ratio = float(e.get("break_even_ratio", 0.97))

    trades = bo.load_trades()
    bars1m = bo.load_bars()   # 1m 缓存用于模拟路径 + 1m ATR

    ex = bs.build_exchange(cfg)
    syms = sorted({t["symbol"] for t in trades})

    # 拉取各周期 K 线（15m / 1h / 4h）
    tf_bars = {tf: {} for tf in ("15m", "1h", "4h")}
    for sym in syms:
        ts = [t for t in trades if t["symbol"] == sym]
        since = int(min(t["opened_at"] for t in ts) * 1000) - 7 * 24 * 3600_000  # 多拉 7 天用于 ATR
        until = int(time.time() * 1000)
        for tf in tf_bars:
            tf_bars[tf][sym] = fetch_tf_bars(ex, sym, tf, since, until)
    print("K 线拉取完成\n")

    # 对每笔交易，分别算各周期 ATR（开仓前 15 根），跑 simulate
    results = {tf: [] for tf in ("1m", "15m", "1h", "4h")}
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in bars1m.get(sym, []) if ts >= opened_ms]

        atrs = {}
        # 1m ATR
        pre1m = [b for b in bars1m.get(sym, []) if b[0] < opened_ms][-15:]
        atrs["1m"] = bo.atr_from_bars(pre1m)
        # 15m / 1h / 4h ATR
        for tf in ("15m", "1h", "4h"):
            pre = [b for b in tf_bars[tf].get(sym, []) if b[0] < opened_ms][-15:]
            atrs[tf] = bo.atr_from_bars(pre)

        for tf in ("1m", "15m", "1h", "4h"):
            atr = atrs.get(tf)
            prep = {"side": side, "entry": entry, "atr": atr, "bars": after}
            pnl = bo.simulate(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                              trail1, trail2, trail2_after, be_ratio)
            results[tf].append(pnl)

    def stats(pnls):
        n = len(pnls)
        total = sum(pnls)
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x <= 0]
        return {"total": total, "wr": len(wins)/n*100 if n else 0,
                "pf": (sum(wins)/abs(sum(losses))) if losses else float("inf"),
                "avg_atr_pnl": total/n if n else 0}

    print(f"回测 {len(trades)} 笔，对比不同止损周期 ATR（其余参数一致）\n")
    print("=" * 62)
    print(f"{'止损周期':<10}{'总盈亏':<12}{'胜率':<10}{'盈亏比':<10}")
    print("=" * 62)
    best_tf, best_total = "1m", -1e9
    for tf in ("1m", "15m", "1h", "4h"):
        s = stats(results[tf])
        mark = ""
        if s["total"] > best_total:
            best_tf, best_total = tf, s["total"]
        print(f"{tf:<10}{s['total']:<+12.2f}{s['wr']:<10.1f}{s['pf']:<10.2f}")
    print("=" * 62)
    print(f"\n最优止损周期 = {best_tf}（总盈亏 {best_total:+.2f}）")
    print(f"当前配置 sl_timeframe = {e.get('sl_timeframe', '1h')}")


if __name__ == "__main__":
    main()
