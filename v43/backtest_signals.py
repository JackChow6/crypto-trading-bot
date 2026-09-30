#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_signals.py —— 回测今天上午的「全部信号」（decision_log 有精确时间戳）。

用 decision_log.jsonl 的每个 decision（含 ts/symbol/action/entry/sl/tp），
按时间顺序重放，维护仓位上限约束，对比：
  A. 固定止盈（持仓久占名额）
  B. 时间衰减止盈（持仓久且未盈利尽早离场，释放名额接新信号）
"""
from __future__ import annotations
import json, os, sys, time
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
DECISION_LOG = os.path.join(BASE, "decision_log.jsonl")
NOTIONAL = 100.0
FEE_PCT = 0.05
MAX_SLOTS = 10


def load_decisions():
    """读 decision_log，返回按时间排序的 decision 列表。"""
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


def load_outcomes():
    """读 outcome，建立 (symbol, entry) -> exit/pnl 的映射，用于验证。"""
    outs = []
    with open(DECISION_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except Exception:
                continue
            if d.get("type") == "outcome":
                outs.append(d)
    return outs


def pnl_fixed(side, entry, exit_px, notional=NOTIONAL):
    if side == "long":
        gross = (exit_px - entry) / entry * notional
    else:
        gross = (entry - exit_px) / entry * notional
    return gross - notional * FEE_PCT / 100 * 2


def main():
    decs = load_decisions()
    print(f"decision_log 共 {len(decs)} 个决策信号\n")

    # 只看今天上午：用当前时间往前推
    now = time.time()
    today_start = now - (now % 86400)
    morning = [d for d in decs if d.get("ts", 0) >= today_start]
    print(f"今天上午的决策信号: {len(morning)} 个\n")

    # 统计方向
    from collections import Counter
    print(f"方向分布: {dict(Counter(d.get('action') for d in morning))}")

    # 关键：这些信号里，有多少最终成交了（有对应 outcome）？
    outs = load_outcomes()
    out_keys = {(o.get("symbol"), round(o.get("entry", 0), 4)) for o in outs}
    matched = 0
    for d in morning:
        key = (d.get("symbol"), round(d.get("entry", 0), 4))
        if key in out_keys:
            matched += 1
    print(f"有成交记录的信号: {matched} / {len(morning)}")
    print(f"（其余 {len(morning)-matched} 个可能是被拒或未触发）\n")

    # 分析：这些信号如果按固定止盈/止损，各自的预期盈亏
    # 从 decision 的 entry/sl/tp 直接算盈亏比
    print("=== 信号自带的止盈止损（entry/sl/tp）===")
    print(f"{'symbol':<20}{'action':<8}{'entry':<12}{'sl':<12}{'tp':<12}{'盈亏比'}")
    print("-" * 70)
    for d in morning[:20]:
        sym = d.get("symbol", "?")
        action = d.get("action", "?")
        entry = d.get("entry", 0)
        sl = d.get("sl", 0)
        tp = d.get("tp", 0)
        if entry and sl and tp:
            if action == "LONG":
                rr = (tp - entry) / (entry - sl) if entry > sl else 0
            else:
                rr = (entry - tp) / (sl - entry) if sl > entry else 0
            print(f"{sym:<20}{action:<8}{entry:<12.6g}{sl:<12.6g}{tp:<12.6g}{rr:.2f}")
    print("-" * 70)


if __name__ == "__main__":
    main()
