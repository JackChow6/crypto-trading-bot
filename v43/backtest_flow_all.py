#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_flow_all.py —— 用上午全部信号（含被拒的）分析量价特征与盈亏的关系。

被拒的信号没有 outcome，用真实 K 线模拟「如果进场」的盈亏（当前止盈止损参数），
这样上午 116 个信号全部纳入分析，样本更完整。
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
NOTIONAL = 100.0
FEE_PCT = 0.05


def load_decisions():
    decs = []
    with open(DECISION_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except Exception:
                continue
            if d.get("type") == "decision":
                decs.append(d)
    decs.sort(key=lambda d: d.get("ts", 0))
    return decs


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def simulate(side, entry, bars, tp_usd, sl_usd):
    """固定止盈止损，返回 pnl。"""
    tp_dist = entry * (tp_usd / NOTIONAL)
    sl_dist = entry * (sl_usd / NOTIONAL)
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        for ts, o, h, l, c in bars:
            if l <= sl:
                return pnl_of(side, entry, sl)
            if h >= tp:
                return pnl_of(side, entry, tp)
        return pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        for ts, o, h, l, c in bars:
            if h >= sl:
                return pnl_of(side, entry, sl)
            if l <= tp:
                return pnl_of(side, entry, tp)
        return pnl_of(side, entry, bars[-1][4] if bars else entry)


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    e = cfg["engine"]
    tp_usd = float(e.get("fixed_tp_usd", 2.4))
    sl_base = float(e.get("fixed_sl_usd", 0.52))

    decs = load_decisions()
    now = time.time()
    today_start = now - (now % 86400)
    morning = [d for d in decs if d.get("ts", 0) >= today_start]
    print(f"上午 decision 共 {len(morning)} 条（含被拒的），全部纳入分析\n")

    pairs = []
    for d in morning:
        sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry"); ts = d.get("ts", 0)
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        side = action.lower()
        ts_ms = int(ts * 1000)

        # 重建特征（ts 之前的 15m K 线）
        feat = {}
        try:
            candles = ex.fetch_ohlcv(sym, "15m", since=ts_ms - 24 * 3600_000, limit=200)
            candles = [c for c in candles if c[0] <= ts_ms]
            if candles:
                snap = features.build_snapshot({"symbol": sym, "ohlcv": candles, "last": entry})
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
                    "ob_imbalance": (snap.get("orderbook") or {}).get("imbalance"),
                }
        except Exception:
            pass

        # 模拟盈亏（ts 之后的 1m K 线）
        try:
            after = ex.fetch_ohlcv(sym, "1m", since=ts_ms, limit=600)
            after = [(b[0], b[1], b[2], b[3], b[4]) for b in after if b[0] >= ts_ms]
        except Exception:
            after = []
        if not after:
            continue
        pnl = simulate(side, entry, after, tp_usd, sl_base)
        pairs.append((feat, pnl))

    print(f"成功配对 {len(pairs)} 条\n")
    wins = [(f, p) for f, p in pairs if p > 0]
    losses = [(f, p) for f, p in pairs if p <= 0]
    print(f"盈利 {len(wins)} 笔 / 亏损 {len(losses)} 笔（胜率 {len(wins)/len(pairs)*100:.1f}%）\n")

    def avg(feats, key):
        vals = [f.get(key) for f in feats if f.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    numeric_keys = ["vol_ratio", "rsi14", "atr_pct", "macd_hist", "ob_imbalance"]
    print("连续型特征对比（盈利单均值 vs 亏损单均值）：")
    print(f"{'特征':<18}{'盈利单均值':<14}{'亏损单均值':<14}{'样本(盈/亏)'}")
    print("-" * 62)
    for k in numeric_keys:
        w_avg = avg([f for f, p in wins], k)
        l_avg = avg([f for f, p in losses], k)
        wc = sum(1 for f, p in wins if f.get(k) is not None)
        lc = sum(1 for f, p in losses if f.get(k) is not None)
        if w_avg is None and l_avg is None:
            continue
        if w_avg is not None and l_avg is not None:
            diff = "盈利高↑" if w_avg > l_avg else "盈利低↓"
        else:
            diff = "数据不足"
        ws = f"{w_avg:.3f}" if w_avg is not None else "N/A"
        ls = f"{l_avg:.3f}" if l_avg is not None else "N/A"
        print(f"{k:<18}{ws:<14}{ls:<14}{wc}/{lc} {diff}")
    print("-" * 62)

    cat_keys = ["vol_trend", "trend_short", "trend_mid", "trend_long", "pullback", "breakdown", "vol_spike"]
    print("\n离散型特征分布（盈利笔数 / 亏损笔数 / 胜率）：")
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
            wc = w_dist.get(v, 0); lc = l_dist.get(v, 0)
            total = wc + lc
            wr = wc / total * 100 if total else 0
            print(f"    {v:<12} 盈 {wc} / 亏 {lc}（胜率 {wr:.0f}%）")


if __name__ == "__main__":
    main()
