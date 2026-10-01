#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_ma99_reverse.py —— 回测「MA99 趋势 + 共振信号反向」规则

规则：
  共振信号(趋势三共振)给出 LONG，但 MA99 趋势为空 → 反向下单(做空)
  共振信号给出 SHORT，但 MA99 趋势为多 → 反向下单(做多)

对比三组（同一批信号触发点，固定止盈止损 2u/3u，名义 100u）：
  A. 反向规则（MA99 与共振矛盾 → 按 MA99 方向反向）
  B. 顺向规则（MA99 与共振一致 → 按共振方向顺向）
  C. 无过滤   （不看 MA99，纯共振方向）

方法：
  拉活跃币 15m K 线，滑动窗口重建 snapshot（Supertrend + EMA20/50 + 结构 + 事件），
  跑 rule_signal.decide 得共振方向；算 MA99(99 根 15m 收盘)趋势。
  开仓后逐根 15m bar 模拟固定止盈止损（保守：先判止损再判止盈）。

用法：python v43/backtest_ma99_reverse.py [--coins 60] [--days 7]
"""
from __future__ import annotations

import argparse
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
    """bars: 开仓后的 [(ts,o,h,l,c),...]。保守先判止损再判止盈。返回 pnl 或 None(未触及)。"""
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
    """MA99 趋势：收盘价 vs 99 均线。>MA99=多，<MA99=空。数据不足返回 None。"""
    if len(closes) < MA99_PERIOD:
        return None
    ma = sum(closes[-MA99_PERIOD:]) / MA99_PERIOD
    return "LONG" if closes[-1] > ma else "SHORT"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=60)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)

    # 1) 活跃币（24h 成交额排序）
    print(f"[1] 拉全市场 ticker，按 24h 成交额取前 {args.coins} ...")
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
    print(f"    候选 {len(pool)} 个币")

    # 2) 拉 15m K 线（7 天 = 672 根，单次 limit 够）
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
    print(f"    有效 {len(bars_map)} 个币")

    # 3) 滑动窗口重建信号 + MA99，收集触发点
    print("[3] 扫描信号 ...")
    sig_reverse = []   # MA99 与共振矛盾 → 反向
    sig_with = []      # MA99 与共振一致 → 顺向
    sig_all = []       # 所有共振信号（无过滤）
    sig_count = {"LONG": 0, "SHORT": 0}

    for sym, bars in bars_map.items():
        closes = [c[4] for c in bars]
        # 从第 120 根开始扫（保证 MA99 + 所有指标 warmup）
        for i in range(120, len(bars) - 1):
            window = [[b[0], b[1], b[2], b[3], b[4], 0] for b in bars[:i + 1]]
            wcloses = [c[4] for c in window]
            snap = features.build_snapshot({"symbol": sym, "ohlcv": window, "last": wcloses[-1]})
            # 历史回测无逐笔成交，orderflow 置空，避免 taker_buy_ratio 默认 0.5 让"买卖主导"恒真
            snap["orderflow"] = {"taker_buy_ratio": None, "cvd_trend": "FLAT"}
            evs = events.detect(snap)
            d = rule_signal.decide(snap, evs, {"trend_min_score": 4})
            if d.action not in ("LONG", "SHORT"):
                continue
            sig_count[d.action] += 1
            entry = wcloses[-1]
            after = [(b[0], b[1], b[2], b[3], b[4]) for b in bars[i + 1:]]
            ma = ma99_trend(wcloses)
            rec = {"sym": sym, "res": d.action, "entry": entry, "after": after}
            sig_all.append(rec)
            if ma is None:
                continue
            if (d.action == "LONG" and ma == "SHORT") or (d.action == "SHORT" and ma == "LONG"):
                sig_reverse.append(rec)
            else:
                sig_with.append(rec)

    print(f"    共振信号总数 {len(sig_all)}（LONG {sig_count['LONG']} / SHORT {sig_count['SHORT']}）")
    print(f"    与 MA99 矛盾(反向候选) {len(sig_reverse)} 个")
    print(f"    与 MA99 一致(顺向候选) {len(sig_with)} 个")

    # 4) 模拟三组盈亏
    def run(sigs, flip):
        pnls = []
        for rec in sigs:
            side = rec["res"].lower()
            if flip:
                side = "short" if side == "long" else "long"
            p = simulate_fixed(side, rec["entry"], rec["after"])
            if p is not None:
                pnls.append(p)
        return pnls

    def stats(pnls, label):
        n = len(pnls)
        if n == 0:
            print(f"  {label}: 无样本")
            return
        total = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / n * 100
        pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
        # 最大连亏
        cur = 0.0
        max_dd = 0.0
        for p in pnls:
            cur = cur + p if p < 0 else 0.0
            max_dd = min(max_dd, cur)
        print(f"  {label:<16} n={n:<4} 总盈亏{total:+9.2f}  胜率{wr:5.1f}%  盈亏比{pf:5.2f}  "
              f"期望{total/n:+.3f}/笔  最大连亏{max_dd:7.2f}")

    print("\n[4] 模拟结果（固定止盈 2u / 止损 3u，名义 100u）")
    print("-" * 90)
    pnls_rev = run(sig_reverse, flip=True)      # MA99矛盾 → 反向做
    pnls_with = run(sig_with, flip=False)       # MA99一致 → 顺向做
    pnls_all = run(sig_all, flip=False)         # 全部共振信号 → 顺向做
    pnls_all_rev = run(sig_all, flip=True)      # 全部共振信号 → 反向做（= reverse:true 现状）
    stats(pnls_all_rev, "★现状:全反向")
    stats(pnls_all, "C.全顺向")
    stats(pnls_rev, "A.MA99矛盾反向")
    stats(pnls_with, "B.MA99一致顺向")

    # 5) 额外：反向候选点如果按原共振方向做，盈亏如何（直接对比反向是否更优）
    pnls_rev_noflip = run(sig_reverse, flip=False)
    stats(pnls_rev_noflip, "A反但顺向做")


if __name__ == "__main__":
    main()
