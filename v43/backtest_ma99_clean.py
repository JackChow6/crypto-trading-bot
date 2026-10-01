#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_ma99_clean.py —— 干净回测「共振信号 × 4h MA99」方向规则

币池 = live_state.json 里实盘真实交易过的币（= 实盘动量榜选出的币），
用 15m K线重建共振信号（rule_signal 趋势三共振），4h K线算 MA99 趋势。

对每个共振信号（原始方向 R），按 4h MA99 方向 M，对比三种下单规则：
  A. 用户规则（最终方向 = M：MA99空做空、MA99多做多）
  B. 无条件反向（现状 reverse:true：最终方向 = 反R）
  C. 无反向（最终方向 = R）

固定止盈 2u / 止损 3u，名义 100u。

用法：python v43/backtest_ma99_clean.py
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

import features
import events
import rule_signal

STATE = os.path.join(BASE, "live_state.json")
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


def ma99_trend_4h(closes_before):
    if len(closes_before) < MA99_PERIOD:
        return None
    ma = sum(closes_before[-MA99_PERIOD:]) / MA99_PERIOD
    return "LONG" if closes_before[-1] > ma else "SHORT"


def stats(pnls, label):
    n = len(pnls)
    if n == 0:
        return f"  {label:<20} 无样本"
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    return (f"  {label:<20} n={n:<4} 总盈亏{total:+9.2f} 胜率{wr:5.1f}% "
            f"盈亏比{pf:5.2f} 期望{total/n:+.3f}/笔")


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)

    # 币池 = 实盘真实交易过的币
    with open(STATE, "r", encoding="utf-8") as f:
        trades = json.load(f).get("trades", [])
    syms = sorted({t["symbol"] for t in trades})
    print(f"实盘交易过的币池：{len(syms)} 个\n")

    print(f"[1] 拉 {len(syms)} 币的 15m K线（7天）+ 4h K线（27天）...")
    bars15 = {}
    bars4h = {}
    for sym in syms:
        try:
            o = ex.fetch_ohlcv(sym, "15m", limit=7 * 96 + 24)
            if o and len(o) >= 200:
                bars15[sym] = [(b[0], b[1], b[2], b[3], b[4]) for b in o]
        except Exception:
            pass
        try:
            o4 = ex.fetch_ohlcv(sym, "4h", limit=27 * 6 + 10)
            if o4:
                bars4h[sym] = [(b[0], b[4]) for b in o4]
        except Exception:
            pass
    print(f"    15m {len(bars15)} 币，4h {len(bars4h)} 币\n")

    print("[2] 重建共振信号，按 4h MA99 分类，模拟三种规则...")
    # 每类信号收集：{side_R, side_M, entry, after}
    signals = []
    for sym, bars in bars15.items():
        c4 = bars4h.get(sym, [])
        for i in range(120, len(bars) - 1):
            window = [[b[0], b[1], b[2], b[3], b[4], 0] for b in bars[:i + 1]]
            wcloses = [c[4] for c in window]
            snap = features.build_snapshot({"symbol": sym, "ohlcv": window, "last": wcloses[-1]})
            snap["orderflow"] = {"taker_buy_ratio": None, "cvd_trend": "FLAT"}
            evs = events.detect(snap)
            d = rule_signal.decide(snap, evs, {"trend_min_score": 4})
            if d.action not in ("LONG", "SHORT"):
                continue
            ts = bars[i][0]
            c4_before = [c for t, c in c4 if t <= ts]
            ma = ma99_trend_4h(c4_before)
            if ma is None:
                continue
            entry = wcloses[-1]
            after = [(b[0], b[1], b[2], b[3], b[4]) for b in bars[i + 1:]]
            signals.append({"R": d.action.lower(), "M": ma.lower(), "entry": entry, "after": after})

    print(f"    重建共振信号 {len(signals)} 个\n")

    # 规则 A：用户规则（最终方向 = M）
    # 规则 B：无条件反向（最终方向 = 反R）→ 现状
    # 规则 C：无反向（最终方向 = R）
    pnls_A = []
    pnls_B = []
    pnls_C = []
    for s in signals:
        side_M = s["M"]                    # 用户规则方向
        side_B = "short" if s["R"] == "long" else "long"   # 反R
        side_C = s["R"]                    # R
        for lst, side in ((pnls_A, side_M), (pnls_B, side_B), (pnls_C, side_C)):
            p = simulate_fixed(side, s["entry"], s["after"])
            if p is not None:
                lst.append(p)

    print("[3] 三种规则对比（固定 2u/3u，名义 100u）：")
    print("-" * 78)
    print(stats(pnls_A, "A.用户规则(方向=MA99)"))
    print(stats(pnls_B, "B.无条件反向(现状)"))
    print(stats(pnls_C, "C.无反向(方向=共振)"))

    # 四象限：用户规则里「共振做多」的两个场景
    print("\n[4] 用户规则的两个具体场景（共振做多时）：")
    print("-" * 78)
    for (r, m) in (("long", "short"), ("long", "long")):
        sub = [s for s in signals if s["R"] == r and s["M"] == m]
        pnls_user = [p for p in (simulate_fixed(m, s["entry"], s["after"]) for s in sub) if p is not None]
        pnls_rev = [p for p in (simulate_fixed("short" if r == "long" else "long", s["entry"], s["after"]) for s in sub) if p is not None]
        tag = "反向做空" if m == "short" else "顺势做多"
        print(f"■ 共振做多 + MA99{'空' if m=='short' else '多'}（{len(sub)}个信号）→ 用户规则{tag}")
        print(stats(pnls_user, f"  用户规则({tag})"))
        print(stats(pnls_rev, f"  无条件反向(做{'空' if r=='long' else '多'})"))
        print()


if __name__ == "__main__":
    main()
