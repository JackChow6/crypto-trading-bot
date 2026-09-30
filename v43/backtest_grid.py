#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量回测：多组「固定止盈/止损」参数 × 正向/反向，输出完整对照表。

用实盘历史成交（decision_log.jsonl）重建每笔持仓的价格路径（币安 1m K 线），
K 线只拉一次并缓存，多组参数复用。
"""
import json
import sys
import time

import ccxt

LOG = "decision_log.jsonl"
PROXY = "http://127.0.0.1:7897"

# 要测试的 (止盈u, 止损u) 参数组
GRID = [
    (1.0, 0.5),
    (2.0, 1.0),
    (3.0, 1.0),
    (2.0, 0.5),
    (3.0, 1.5),
    (1.5, 0.5),
    (2.0, 2.0),
    (1.0, 1.0),
]


def load_outcomes():
    outs = []
    with open(LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "outcome":
                outs.append(rec)
    return outs


def infer_notional(entry, exit_, pnl):
    if entry == 0:
        return None
    move = abs(exit_ - entry) / entry
    if move == 0:
        return None
    return abs(pnl) / move


def fetch_klines(ex, recs):
    """拉取所有成交的持仓期 K 线，返回 {(sym, close_ts): ohlcv}。"""
    cache = {}
    for i, rec in enumerate(recs):
        sym = rec["symbol"]
        close_ts = float(rec["ts"])
        key = (sym, close_ts)
        if key in cache:
            continue
        since_ms = int((close_ts - 24 * 3600) * 1000)
        try:
            ohlcv = ex.fetch_ohlcv(sym, "1m", since=since_ms, limit=1440)
            cache[key] = ohlcv
        except Exception:
            cache[key] = None
        if (i + 1) % 30 == 0:
            print(f"  拉取K线进度 {i+1}/{len(recs)}", flush=True)
    return cache


def locate_start(ohlcv, entry):
    start_i = 0
    best = None
    for i, c in enumerate(ohlcv):
        cclose = float(c[4])
        d = abs(cclose - entry) / entry if entry else 1e9
        if best is None or d < best[0]:
            best = (d, i)
    if best and best[0] < 0.05:
        return best[1]
    return 0


def simulate(ohlcv, side, entry, close_ts, tp_price, sl_price):
    """返回 'TP'/'SL'/'AMB'/None（None=未触及）。"""
    start_i = locate_start(ohlcv, entry)
    for i in range(start_i, len(ohlcv)):
        c = ohlcv[i]
        high = float(c[2])
        low = float(c[3])
        t = c[0] / 1000.0
        if t > close_ts + 60:
            break
        if side == "long":
            hit_tp = high >= tp_price
            hit_sl = low <= sl_price
        else:
            hit_tp = low <= tp_price
            hit_sl = high >= sl_price
        if hit_sl and not hit_tp:
            return "SL"
        if hit_tp and not hit_sl:
            return "TP"
        if hit_tp and hit_sl:
            return "AMB"
    return None


def run_one(recs, cache, reverse, tp_usd, sl_usd):
    """对一组参数回测，返回 (tp_n, sl_n, amb_n, none_n, sum_pess, sum_opt, sample)。"""
    tp_n = sl_n = amb_n = none_n = 0
    sum_pess = 0.0
    sum_opt = 0.0
    sample = 0
    for rec in recs:
        sym = rec["symbol"]
        side = rec["side"]
        entry = float(rec["entry"])
        exit_ = float(rec["exit"])
        pnl = float(rec["pnl"])
        close_ts = float(rec["ts"])
        if reverse:
            side = "short" if side == "long" else "long"
            pnl = -pnl
        notional = infer_notional(entry, exit_, pnl)
        if not notional or notional <= 0:
            continue
        if side == "long":
            tp_price = entry * (1 + tp_usd / notional)
            sl_price = entry * (1 - sl_usd / notional)
        else:
            tp_price = entry * (1 - tp_usd / notional)
            sl_price = entry * (1 + sl_usd / notional)
        ohlcv = cache.get((sym, close_ts))
        if ohlcv is None:
            continue
        res = simulate(ohlcv, side, entry, close_ts, tp_price, sl_price)
        sample += 1
        if res == "TP":
            tp_n += 1
            sum_pess += tp_usd
            sum_opt += tp_usd
        elif res == "SL":
            sl_n += 1
            sum_pess += -sl_usd
            sum_opt += -sl_usd
        elif res == "AMB":
            amb_n += 1
            sum_pess += -sl_usd
            sum_opt += tp_usd
        else:
            none_n += 1
            sum_pess += pnl
            sum_opt += pnl
    return tp_n, sl_n, amb_n, none_n, sum_pess, sum_opt, sample


def main():
    recs = load_outcomes()
    print(f"共 {len(recs)} 笔成交，测试参数组 {len(GRID)} 组 × 正向/反向\n")
    ex = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "proxies": {"http": PROXY, "https": PROXY},
    })
    print("拉取历史 K 线（缓存复用）...")
    cache = fetch_klines(ex, recs)
    print("K 线拉取完成\n")

    header = f"{'模式':<8}{'止盈u':>6}{'止损u':>6}{'样本':>6}{'TP':>6}{'SL':>6}{'AMB':>6}{'未触':>6}{'胜率%':>7}{'悲观':>9}{'乐观':>9}"
    print(header)
    print("-" * len(header))

    results = []
    for reverse in (False, True):
        mode = "反向" if reverse else "正向"
        for tp_usd, sl_usd in GRID:
            tp_n, sl_n, amb_n, none_n, sp, so, sample = run_one(recs, cache, reverse, tp_usd, sl_usd)
            if sample == 0:
                continue
            win = tp_n / sample * 100
            print(f"{mode:<8}{tp_usd:>6.1f}{sl_usd:>6.1f}{sample:>6}{tp_n:>6}{sl_n:>6}{amb_n:>6}{none_n:>6}{win:>7.1f}{sp:>9.2f}{so:>9.2f}")
            results.append((mode, tp_usd, sl_usd, sample, tp_n, sl_n, sp, so, win))

    print("\n" + "=" * 60)
    print("按「悲观口径总盈亏」排序（反向模式）:")
    rev = [r for r in results if r[0] == "反向"]
    rev.sort(key=lambda x: x[6], reverse=True)
    for mode, tp, sl, sample, tpn, sln, sp, so, win in rev[:5]:
        print(f"  止盈{tp:.1f}u/止损{sl:.1f}u: 悲观 {sp:.2f}u / 乐观 {so:.2f}u (胜率{win:.1f}%, 样本{sample})")


if __name__ == "__main__":
    main()
