#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_flow_features.py —— 分析量价/订单流特征与盈亏的关系。

数据源：decision_log.jsonl 的 decision（含 feat 特征）+ outcome（含 pnl）。
方法：把每个 decision 的开仓特征 与 后续 outcome 的 pnl 配对，
统计「盈利单 vs 亏损单」在各特征上的分布差异，找出能区分盈利的特征及阈值。
"""
from __future__ import annotations
import json, os, sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

DECISION_LOG = os.path.join(BASE, "decision_log.jsonl")


def load_all():
    decisions = []
    outcomes = []
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
    decisions, outcomes = load_all()
    print(f"decision {len(decisions)} 条，outcome {len(outcomes)} 条\n")

    # 有 feat 特征的 decision 数量
    with_feat = [d for d in decisions if d.get("feat")]
    print(f"含量价/订单流特征的 decision: {len(with_feat)} 条")

    if len(with_feat) < 20:
        print("\n⚠️ 特征数据还不足（<20 条），需要让引擎跑一段时间积累。")
        print("当前引擎已改为记录特征，几小时后数据够了再跑本脚本。")
        return

    # 配对：decision 的 symbol+entry ≈ outcome 的 symbol+entry，取最近时间的 outcome
    # 简化：按 symbol 聚合，把同一 symbol 的 outcome pnl 平均，看该 symbol 的 decision 特征
    # 更准确的做法：按时间配对，decision 后最近的 outcome
    outcomes_by_sym = defaultdict(list)
    for o in outcomes:
        outcomes_by_sym[o.get("symbol")].append(o)

    # 每个 decision 关联一个 pnl（同 symbol 的 outcome 里，entry 最接近的）
    pairs = []  # (feat_dict, pnl)
    for d in with_feat:
        sym = d.get("symbol")
        entry = d.get("entry", 0)
        cands = outcomes_by_sym.get(sym, [])
        if not cands:
            continue
        # 找 entry 最接近的 outcome
        best = min(cands, key=lambda o: abs(o.get("entry", 0) - entry))
        pairs.append((d["feat"], best.get("pnl", 0)))

    print(f"\n成功配对 {len(pairs)} 条（decision 特征 ↔ 盈亏）\n")

    if len(pairs) < 10:
        print("配对样本不足，继续积累数据")
        return

    # 分组统计各特征
    wins = [(f, p) for f, p in pairs if p > 0]
    losses = [(f, p) for f, p in pairs if p <= 0]
    print(f"盈利 {len(wins)} 笔 / 亏损 {len(losses)} 笔\n")

    def avg(feats, key):
        vals = [f.get(key) for f in feats if f.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    # 连续型特征：盈利 vs 亏损的均值对比
    numeric_keys = ["taker_buy_ratio", "delta_5m", "vol_ratio", "rsi14", "atr_pct",
                    "macd_hist", "ob_imbalance", "large_trades"]
    print("连续型特征对比（盈利单均值 vs 亏损单均值）：")
    print(f"{'特征':<20}{'盈利单均值':<14}{'亏损单均值':<14}{'差异方向'}")
    print("-" * 60)
    for k in numeric_keys:
        w_avg = avg([f for f, p in wins], k)
        l_avg = avg([f for f, p in losses], k)
        if w_avg is None and l_avg is None:
            continue
        if w_avg is None or l_avg is None:
            diff = "数据不足"
        elif w_avg > l_avg:
            diff = "盈利单更高 ↑"
        elif w_avg < l_avg:
            diff = "盈利单更低 ↓"
        else:
            diff = "无差异"
        ws = f"{w_avg:.3f}" if w_avg is not None else "N/A"
        ls = f"{l_avg:.3f}" if l_avg is not None else "N/A"
        print(f"{k:<20}{ws:<14}{ls:<14}{diff}")
    print("-" * 60)

    # 离散型特征：cvd_trend / cvd_direction / vol_trend / trend_short / trend_mid
    print("\n离散型特征：盈利单 vs 亏损单的取值分布")
    cat_keys = ["cvd_trend", "cvd_direction", "vol_trend", "trend_short", "trend_mid"]
    for k in cat_keys:
        w_dist = defaultdict(int)
        l_dist = defaultdict(int)
        for f, p in wins:
            v = f.get(k)
            if v: w_dist[v] += 1
        for f, p in losses:
            v = f.get(k)
            if v: l_dist[v] += 1
        if not w_dist and not l_dist:
            continue
        print(f"\n  {k}:")
        all_vals = set(w_dist) | set(l_dist)
        for v in sorted(all_vals):
            wc = w_dist.get(v, 0)
            lc = l_dist.get(v, 0)
            wr = wc / len(wins) * 100 if wins else 0
            lr = lc / len(losses) * 100 if losses else 0
            print(f"    {v:<12} 盈利单 {wc}笔({wr:.0f}%)  亏损单 {lc}笔({lr:.0f}%)")


if __name__ == "__main__":
    main()
