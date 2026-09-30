#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
特征分析：把 decision_log 的 feat（开仓时量价/订单流特征）关联到 live_state 的实际盈亏，
对比「盈利单 vs 亏损单」在各特征上的差异，找出能区分盈亏的信号特征。
"""
import json, os, sys, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
DEC = os.path.join(BASE, "decision_log.jsonl")
LIVE = os.path.join(BASE, "live_state.json")

# 1) 读 decision（带 feat 的）
decs = []
with open(DEC, "r", encoding="utf-8") as f:
    for line in f:
        try:
            d = json.loads(line.strip())
        except Exception:
            continue
        if d.get("type") == "decision" and d.get("feat"):
            decs.append(d)
decs.sort(key=lambda d: d.get("ts", 0))

# 2) 读 trades
trades = json.load(open(LIVE, encoding="utf-8"))["trades"]

# 3) 关联：decision -> trade（同 symbol + 同方向 + 时间接近：trade.opened_at 在 decision.ts 之后 5 分钟内）
# 每个 trade 找最近的一个同 symbol 同方向的 decision（时间 >= opened_at 前 2 分钟）
pairs = []
used = set()
for t in trades:
    sym = t["symbol"]; side = t["side"]; opened = t.get("opened_at", 0)
    best = None; best_dt = 1e18
    for d in decs:
        if d.get("symbol") != sym:
            continue
        if d.get("action", "").lower() != side:
            continue
        dt = opened - d.get("ts", 0)
        if dt < -120 or dt > 300:   # 决策在开仓前 2 分钟 ~ 开仓后 5 分钟内
            continue
        if abs(dt) < abs(best_dt):
            best_dt = dt
            best = d
    if best is not None:
        pairs.append((best, t))

print(f"关联到 {len(pairs)} 个「决策→成交」对（共 {len(trades)} 笔成交）")

# 4) 分盈利/亏损组，对比特征
wins = [(d, t) for d, t in pairs if t["pnl"] > 0]
losses = [(d, t) for d, t in pairs if t["pnl"] <= 0]
print(f"盈利 {len(wins)} 笔，亏损 {len(losses)} 笔")

# 数值特征
num_feats = ["taker_buy_ratio", "delta_5m", "delta_1m", "large_trades",
             "vol_ratio", "rsi14", "atr_pct", "macd_hist", "ob_imbalance"]

def mean(rows, key):
    vals = []
    for d, t in rows:
        v = d["feat"].get(key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    return statistics.mean(vals) if vals else None

def cat_dist(rows, key):
    dist = {}
    for d, t in rows:
        v = d["feat"].get(key)
        if v is None:
            continue
        dist[str(v)] = dist.get(str(v), 0) + 1
    return dist

print("\n========== 数值特征：盈利单 vs 亏损单 ==========")
print(f"{'特征':<18}{'盈利均值':>12}{'亏损均值':>12}{'差异':>12}")
for key in num_feats:
    wm = mean(wins, key); lm = mean(losses, key)
    if wm is None or lm is None:
        continue
    diff = wm - lm
    print(f"{key:<18}{wm:>12.4f}{lm:>12.4f}{diff:>+12.4f}")

print("\n========== 分类特征：盈利单 vs 亏损单 分布 ==========")
for key in ["cvd_trend", "cvd_direction", "vol_trend", "trend_short", "trend_mid"]:
    wd = cat_dist(wins, key); ld = cat_dist(losses, key)
    print(f"\n[{key}]")
    allk = sorted(set(list(wd.keys()) + list(ld.keys())))
    for k in allk:
        w = wd.get(k, 0); l = ld.get(k, 0)
        print(f"  {k:<10} 盈利 {w:<4} 亏损 {l:<4}")
