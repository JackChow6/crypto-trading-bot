#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_scan.py —— 参数网格扫描，找实盘记录区间内的最优止盈/止损设置。

扫描维度：
  - 止盈金额 tp_usd
  - 止损金额 sl_usd
  - 是否动态止盈 dynamic（False=固定止盈立即平仓，True=触及止盈锁利继续跑）
  - 动态模式下的移动止损参数 be_ratio / trail_ratio

用币安真实 1m K 线重放每笔实盘成交的价格路径，按总净盈亏排序输出最优组合。
K 线缓存到磁盘，避免重复拉取。
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

STATE = os.path.join(BASE, "live_state.json")
CONFIG = os.path.join(ROOT, "config.yaml")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")

NOTIONAL = 100.0
FEE_PCT = 0.05   # 单边 0.05%，开平各一次


def build_exchange(cfg: dict):
    xc = cfg["exchange"]
    params = {"enableRateLimit": True,
              "options": {"defaultType": xc.get("default_type", "swap")}}
    if xc.get("api_key"):
        params["apiKey"] = xc["api_key"]
        params["secret"] = xc["api_secret"]
    if xc.get("proxy"):
        params["proxies"] = {"http": xc["proxy"], "https": xc["proxy"]}
    ex = getattr(ccxt, xc["name"])(params)
    ex.load_markets()
    return ex


def load_trades():
    with open(STATE, "r", encoding="utf-8") as f:
        return json.load(f).get("trades", [])


def fetch_bars(ex, symbol, since_ms, until_ms, timeframe="1m"):
    out = []
    cursor = since_ms
    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        except Exception as e:
            print(f"    拉取 {symbol} 失败 @{cursor}: {e}")
            break
        if not bars:
            break
        for b in bars:
            if since_ms <= b[0] <= until_ms:
                out.append([b[0], b[1], b[2], b[3], b[4]])
        last = bars[-1][0]
        if last <= cursor:
            break
        cursor = last + 60_000
    return out


def get_bars_cache(ex, trades):
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("fetched_at", 0) < 3600 * 6:
                print(f"  复用 K 线缓存（{len(d['bars'])} 币种）")
                return d["bars"]
        except Exception:
            pass
    print("  拉取 K 线（无有效缓存）...")
    syms = sorted({t["symbol"] for t in trades})
    bars = {}
    for s in syms:
        ts = [t for t in trades if t["symbol"] == s]
        since = int(min(t["opened_at"] for t in ts) * 1000)
        until = int(time.time() * 1000)
        bars[s] = fetch_bars(ex, s, since, until)
        print(f"    {s}: {len(bars[s])} 根")
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "bars": bars}, f)
    return bars


def simulate(side, entry, bars, tp_usd, sl_usd, dynamic,
             be_ratio=0.5, trail_ratio=0.5):
    """重放策略，返回 (exit_price, reason, bars_held)。"""
    tp_dist = entry * tp_usd / NOTIONAL
    sl_dist = entry * sl_usd / NOTIONAL
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
    else:
        tp, sl = entry - tp_dist, entry + sl_dist

    if not dynamic:
        # 固定止盈止损：保守先判止损，再判止盈
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
        return (bars[-1][4] if bars else entry), "EOD", len(bars)

    # 动态止盈：触及 TP 锁利（SL 移到 TP），移动止损跟进
    be = sl_dist * be_ratio
    trail = sl_dist * trail_ratio
    if side == "long":
        highest = entry
        tp_live = tp
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp_live is not None and h >= tp_live:
                sl = max(sl, tp_live)
                tp_live = None
            highest = max(highest, h)
            if highest >= entry + be:
                sl = max(sl, entry)
                sl = max(sl, highest - trail)
            if l <= sl:
                return sl, "SL", i + 1
            if c <= sl:
                return sl, "SL", i + 1
        return (bars[-1][4] if bars else entry), "EOD", len(bars)
    else:
        lowest = entry
        tp_live = tp
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp_live is not None and l <= tp_live:
                sl = min(sl, tp_live)
                tp_live = None
            lowest = min(lowest, l)
            if lowest <= entry - be:
                sl = min(sl, entry)
                sl = min(sl, lowest + trail)
            if h >= sl:
                return sl, "SL", i + 1
            if c >= sl:
                return sl, "SL", i + 1
        return (bars[-1][4] if bars else entry), "EOD", len(bars)


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def evaluate(trades, bars_cache, tp_usd, sl_usd, dynamic, be_ratio=0.5, trail_ratio=0.5):
    """跑一个参数组合，返回统计 dict。"""
    pnls = []
    wins = 0
    tp_count = 0
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        allbars = bars_cache.get(sym, [])
        bars = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
        exit_px, reason, held = simulate(side, entry, bars, tp_usd, sl_usd, dynamic,
                                         be_ratio, trail_ratio)
        pnl = pnl_of(side, entry, exit_px)
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        if reason == "TP":
            tp_count += 1
    n = len(pnls)
    total = sum(pnls)
    losses = [p for p in pnls if p <= 0]
    wins_list = [p for p in pnls if p > 0]
    # 最大连续亏损（equity 曲线回撤）
    cur = 0.0
    max_dd = 0.0
    for p in pnls:
        cur = cur + p if p < 0 else 0.0
        max_dd = min(max_dd, cur)
    return {
        "tp": tp_usd, "sl": sl_usd, "dynamic": dynamic,
        "be": be_ratio, "trail": trail_ratio,
        "total": round(total, 2),
        "n": n, "wins": wins, "win_rate": round(wins / n * 100, 1),
        "avg_win": round(sum(wins_list) / len(wins_list), 2) if wins_list else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "pf": round(sum(wins_list) / abs(sum(losses)), 2) if losses else float("inf"),
        "expect": round(total / n, 3),
        "max_dd": round(max_dd, 2),
        "tp_count": tp_count,
    }


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)
    trades = load_trades()
    bars_cache = get_bars_cache(ex, trades)
    print(f"\n共 {len(trades)} 笔成交，开始网格扫描...\n")

    tp_grid = [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    sl_grid = [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0]
    results = []
    for dynamic in (False, True):
        for tp in tp_grid:
            for sl in sl_grid:
                r = evaluate(trades, bars_cache, tp, sl, dynamic)
                results.append(r)

    # 按总盈亏排序
    results.sort(key=lambda r: r["total"], reverse=True)

    # 输出 Top 25
    print("=" * 100)
    print(f"{'排名':<4}{'模式':<6}{'止盈':<6}{'止损':<6}{'总盈亏':<9}{'胜率':<8}{'盈亏比':<7}{'期望/笔':<9}{'最大连亏':<9}{'止盈笔数'}")
    print("=" * 100)
    for i, r in enumerate(results[:25]):
        mode = "动态" if r["dynamic"] else "固定"
        print(f"{i+1:<4}{mode:<6}{r['tp']:<6}{r['sl']:<6}{r['total']:<+9}{r['win_rate']:<8}{r['pf']:<7}{r['expect']:<+9}{r['max_dd']:<+9}{r['tp_count']}")

    # 当前配置的位置
    cur = next((r for r in results if abs(r["tp"] - 1.5) < 1e-9 and abs(r["sl"] - 1.5) < 1e-9 and not r["dynamic"]), None)
    if cur:
        rank = results.index(cur) + 1
        print(f"\n当前配置（固定 1.5u/1.5u）排名：第 {rank} / {len(results)}，总盈亏 {cur['total']:+.2f}")

    # 动态模式最优 + 移动止损参数微调
    dyn_top = [r for r in results if r["dynamic"]]
    dyn_top.sort(key=lambda r: r["total"], reverse=True)
    if dyn_top:
        best_dyn = dyn_top[0]
        print(f"\n动态止盈最优（be/trail=0.5）：止盈{best_dyn['tp']}u/止损{best_dyn['sl']}u，总盈亏 {best_dyn['total']:+.2f}")
        # 对动态 top3 做 be/trail 微调
        print("\n对动态模式 Top3 做移动止损参数微调（be_ratio × trail_ratio）：")
        tuned = []
        for bd in dyn_top[:3]:
            for be_r in (0.3, 0.5, 0.7, 1.0):
                for tr_r in (0.3, 0.5, 0.7, 1.0):
                    r = evaluate(trades, bars_cache, bd["tp"], bd["sl"], True, be_r, tr_r)
                    tuned.append(r)
        tuned.sort(key=lambda r: r["total"], reverse=True)
        for r in tuned[:10]:
            print(f"  止盈{r['tp']}u/止损{r['sl']}u be={r['be']} trail={r['trail']} "
                  f"总盈亏{r['total']:+.2f} 胜率{r['win_rate']}% 盈亏比{r['pf']} 期望{r['expect']:+.3f}")

    # 全局最优
    best = results[0]
    print("\n" + "=" * 100)
    print(f"全局最优：{'动态' if best['dynamic'] else '固定'}止盈，止盈{best['tp']}u/止损{best['sl']}u")
    print(f"  总盈亏 {best['total']:+.2f} USDT | 胜率 {best['win_rate']}% | 盈亏比 {best['pf']} | "
          f"期望 {best['expect']:+.3f} USDT/笔 | 最大连亏 {best['max_dd']} | 止盈笔数 {best['tp_count']}")
    print("=" * 100)
    print("注意：样本仅 93 笔且集中在单日，存在过拟合风险，最优参数需小资金实盘验证。")


if __name__ == "__main__":
    main()
