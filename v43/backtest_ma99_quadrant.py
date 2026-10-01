#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_ma99_quadrant.py —— 聚焦验证「MA99 趋势空 + 共振做多 → 反向下单做空」

把共振信号 × MA99 趋势拆成 4 象限，每象限分别模拟「反向做」和「顺向做」，
专门看用户关心的象限：共振做多(LONG) + MA99空(SHORT) 时，做空到底对不对。

用法：python v43/backtest_ma99_quadrant.py [--coins 80] [--days 7]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import ccxt
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import features
import events
import rule_signal

CONFIG = os.path.join(ROOT, "config.yaml")

NOTIONAL = 100.0
FEE_PCT = 0.05
TP_USD = 2.0
SL_USD = 3.0
MA99_PERIOD = 99


def build_exchange(cfg):
    xc = cfg["exchange"]
    params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if xc.get("proxy"):
        params["proxies"] = {"http": xc["proxy"], "https": xc["proxy"]}
    ex = getattr(ccxt, xc["name"])(params)
    ex.load_markets()
    return ex


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def simulate_fixed(side, entry, bars):
    tp_dist = entry * TP_USD / NOTIONAL
    sl_dist = entry * SL_USD / NOTIONAL
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        for ts, o, h, l, c in bars:
            if l <= sl:
                return pnl_of(side, entry, sl)
            if h >= tp:
                return pnl_of(side, entry, tp)
        return None
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        for ts, o, h, l, c in bars:
            if h >= sl:
                return pnl_of(side, entry, sl)
            if l <= tp:
                return pnl_of(side, entry, tp)
        return None


def ma99_trend(closes):
    if len(closes) < MA99_PERIOD:
        return None
    ma = sum(closes[-MA99_PERIOD:]) / MA99_PERIOD
    return "LONG" if closes[-1] > ma else "SHORT"


def stats(pnls, label):
    n = len(pnls)
    if n == 0:
        return f"  {label:<28} 无样本"
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    return (f"  {label:<28} n={n:<4} 总盈亏{total:+9.2f} 胜率{wr:5.1f}% "
            f"盈亏比{pf:5.2f} 期望{total/n:+.3f}/笔")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=80)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)

    print(f"[1] 拉全市场 ticker 取前 {args.coins} ...")
    tickers = ex.fetch_tickers()
    pool = []
    for sym, t in tickers.items():
        if not str(sym).endswith("/USDT:USDT"):
            continue
        qv = float(t.get("quoteVolume") or 0.0)
        if qv >= 1000000:
            pool.append((sym, qv))
    pool.sort(key=lambda x: x[1], reverse=True)
    pool = [s for s, _ in pool[:args.coins]]

    print(f"[2] 拉 {args.days} 天 15m K 线 ...")
    limit = args.days * 96 + 24
    bars_map = {}
    for sym in pool:
        try:
            ohlcv = ex.fetch_ohlcv(sym, "15m", limit=limit)
            if ohlcv and len(ohlcv) >= 200:
                bars_map[sym] = [(b[0], b[1], b[2], b[3], b[4]) for b in ohlcv]
        except Exception:
            pass

    print(f"[3] 扫描信号，按象限分类 ...")
    # 象限：(共振方向, MA99方向) -> list of (entry, after)
    quads = {
        ("LONG", "SHORT"): [],   # 用户关心：共振多 + MA99空
        ("LONG", "LONG"): [],
        ("SHORT", "LONG"): [],
        ("SHORT", "SHORT"): [],
    }
    for sym, bars in bars_map.items():
        closes = [c[4] for c in bars]
        for i in range(120, len(bars) - 1):
            window = [[b[0], b[1], b[2], b[3], b[4], 0] for b in bars[:i + 1]]
            wcloses = [c[4] for c in window]
            snap = features.build_snapshot({"symbol": sym, "ohlcv": window, "last": wcloses[-1]})
            snap["orderflow"] = {"taker_buy_ratio": None, "cvd_trend": "FLAT"}
            evs = events.detect(snap)
            d = rule_signal.decide(snap, evs, {"trend_min_score": 4})
            if d.action not in ("LONG", "SHORT"):
                continue
            ma = ma99_trend(wcloses)
            if ma is None:
                continue
            entry = wcloses[-1]
            after = [(b[0], b[1], b[2], b[3], b[4]) for b in bars[i + 1:]]
            quads[(d.action, ma)].append((entry, after))

    print("\n[4] 各象限信号数量：")
    for k, v in quads.items():
        print(f"  共振{k[0]:<5} + MA99{k[1]:<5}: {len(v)} 个")

    print("\n[5] 各象限分别「反向做」vs「顺向做」盈亏：")
    print("-" * 100)
    for (res, ma), sigs in quads.items():
        side = res.lower()
        flip_side = "short" if side == "long" else "long"
        pnls_flip = [p for p in (simulate_fixed(flip_side, e, a) for e, a in sigs) if p is not None]
        pnls_noflip = [p for p in (simulate_fixed(side, e, a) for e, a in sigs) if p is not None]
        print(f"■ 共振{res:<5} + MA99{ma:<5}（{len(sigs)}个信号）")
        print(stats(pnls_flip, f"  反向做({flip_side})"))
        print(stats(pnls_noflip, f"  顺向做({side})"))
        print()


if __name__ == "__main__":
    main()
