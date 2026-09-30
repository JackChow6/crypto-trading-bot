#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_flow_morning.py —— 用上午的信号数据重建量价特征，分析盈利单 vs 亏损单。

上午的 decision 没有数值特征（feat 是后来才加的），但每个 decision 有 ts/symbol，
可以用币安历史 K 线重建 K 线类特征（vol_ratio/rsi/atr/macd/趋势/回踩），
订单流特征（taker_buy_ratio 等）尝试用逐笔成交拉取（历史可能拉不到则跳过）。

然后配对 outcome 的 pnl，分析盈利单 vs 亏损单的特征差异。
"""
from __future__ import annotations
import json, os, sys, time
from collections import defaultdict
import yaml
import ccxt

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_scan as bs
import features

CONFIG = os.path.join(ROOT, "config.yaml")
DECISION_LOG = os.path.join(BASE, "decision_log.jsonl")


def load_all():
    decisions, outcomes = [], []
    with open(DECISION_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except Exception:
                continue
            if d.get("type") == "decision":
                decisions.append(d)
            elif d.get("type") == "outcome":
                outcomes.append(d)
    return decisions, outcomes


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    decisions, outcomes = load_all()

    now = time.time()
    today_start = now - (now % 86400)
    morning_dec = [d for d in decisions if d.get("ts", 0) >= today_start]
    print(f"上午 decision {len(morning_dec)} 条，outcome 共 {len(outcomes)} 条\n")

    # outcome 按 symbol 建索引
    outcomes_by_sym = defaultdict(list)
    for o in outcomes:
        outcomes_by_sym[o.get("symbol")].append(o)

    pairs = []  # (feat_dict, pnl)
    rebuilt = 0
    for d in morning_dec:
        sym = d.get("symbol"); entry = d.get("entry", 0); ts = d.get("ts", 0)
        if not sym or not entry:
            continue
        # 找该 symbol 最接近 entry 的 outcome pnl
        cands = outcomes_by_sym.get(sym, [])
        if not cands:
            continue
        best = min(cands, key=lambda o: abs(o.get("entry", 0) - entry))
        pnl = best.get("pnl", 0)

        # 重建特征：拉 ts 之前的 15m K 线
        feat = {}
        try:
            ts_ms = int(ts * 1000)
            candles = ex.fetch_ohlcv(sym, "15m", since=ts_ms - 24 * 3600_000, limit=200)
            candles = [c for c in candles if c[0] <= ts_ms]
            if candles:
                snap = features.build_snapshot({"symbol": sym, "ohlcv": candles, "last": entry})
                flow = snap.get("orderflow") or {}
                vol = snap.get("volume") or {}
                feat = {
                    "vol_ratio": snap.get("vol_ratio"),
                    "vol_trend": vol.get("trend"),
                    "vol_spike": bool(vol.get("spike")),
                    "rsi14": snap.get("rsi14"),
                    "atr_pct": snap.get("atr_pct"),
                    "macd_hist": (snap.get("macd") or {}).get("hist"),
                    "trend_short": (snap.get("trend") or {}).get("short"),
                    "trend_mid": (snap.get("trend") or {}).get("mid"),
                    "trend_long": (snap.get("trend") or {}).get("long"),
                    "pullback": snap.get("pullback"),
                    "breakdown": snap.get("breakdown"),
                    "taker_buy_ratio": flow.get("taker_buy_ratio"),
                }
                # 订单流特征（逐笔成交，历史可能拉不到）
                try:
                    trades = ex.fetch_trades(sym, limit=500)
                    trades = [t for t in trades if t.get("timestamp", 0) <= ts_ms]
                    if trades:
                        flow2 = features.delta_cvd(trades)
                        feat["taker_buy_ratio"] = flow2.get("taker_buy_ratio")
                        feat["cvd_trend"] = flow2.get("cvd_trend")
                        feat["delta_5m"] = flow2.get("delta_5m")
                except Exception:
                    pass
                rebuilt += 1
        except Exception as e:
            pass
        pairs.append((feat, pnl))

    print(f"重建特征 {rebuilt} 条，配对 {len(pairs)} 条\n")

    wins = [(f, p) for f, p in pairs if p > 0]
    losses = [(f, p) for f, p in pairs if p <= 0]
    print(f"盈利 {len(wins)} 笔 / 亏损 {len(losses)} 笔\n")

    def avg(feats, key):
        vals = [f.get(key) for f in feats if f.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    numeric_keys = ["taker_buy_ratio", "delta_5m", "vol_ratio", "rsi14", "atr_pct", "macd_hist"]
    print("连续型特征对比（盈利单均值 vs 亏损单均值）：")
    print(f"{'特征':<20}{'盈利单均值':<14}{'亏损单均值':<14}{'样本(盈/亏)'}")
    print("-" * 66)
    for k in numeric_keys:
        w_avg = avg([f for f, p in wins], k)
        l_avg = avg([f for f, p in losses], k)
        wc = sum(1 for f, p in wins if f.get(k) is not None)
        lc = sum(1 for f, p in losses if f.get(k) is not None)
        if w_avg is None and l_avg is None:
            continue
        diff = "盈利高↑" if (w_avg and l_avg and w_avg > l_avg) else ("盈利低↓" if (w_avg and l_avg and w_avg < l_avg) else "无差异")
        ws = f"{w_avg:.3f}" if w_avg is not None else "N/A"
        ls = f"{l_avg:.3f}" if l_avg is not None else "N/A"
        print(f"{k:<20}{ws:<14}{ls:<14}{wc}/{lc} {diff}")
    print("-" * 66)

    cat_keys = ["cvd_trend", "vol_trend", "trend_short", "trend_mid", "trend_long", "pullback", "breakdown"]
    print("\n离散型特征分布：")
    for k in cat_keys:
        w_dist = defaultdict(int); l_dist = defaultdict(int)
        for f, p in wins:
            v = f.get(k)
            if v is not None: w_dist[str(v)] += 1
        for f, p in losses:
            v = f.get(k)
            if v is not None: l_dist[str(v)] += 1
        if not w_dist and not l_dist:
            continue
        print(f"\n  {k}:")
        for v in sorted(set(w_dist) | set(l_dist)):
            print(f"    {v:<10} 盈利 {w_dist.get(v,0)}笔  亏损 {l_dist.get(v,0)}笔")


if __name__ == "__main__":
    main()
