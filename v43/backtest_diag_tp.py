#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断止盈链路：锁利门槛、分级移动止损的触发情况和效果。"""
from __future__ import annotations
import json, os, sys, time
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")


def fetch_tf_bars(ex, sym, timeframe, since_ms, until_ms):
    out, cursor = [], since_ms
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(sym, timeframe, since=cursor, limit=1000)
        except Exception:
            break
        if not bars:
            break
        for b in bars:
            if since_ms <= b[0] <= until_ms:
                out.append([b[0], b[1], b[2], b[3], b[4]])
        last = bars[-1][0]
        if last <= cursor:
            break
        cursor = last + step
    return out


def simulate_detail(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max, trail1, trail2, trail2_after, be_ratio):
    """返回 (pnl, exit_reason, peak_pnl, tp_hit)。"""
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    sl_usd = sl_base
    if atr and entry:
        sl_usd = atr * sl_atr_mult * 100.0 / entry
        sl_usd = max(sl_min, min(sl_max, sl_usd))
    sl_dist = entry * (sl_usd / 100.0)
    tp_dist = entry * (tp_usd / 100.0)
    be = sl_dist * be_ratio
    trail1_d = entry * (trail1 / 100.0)
    trail2_d = entry * (trail2 / 100.0)
    trail2_after_d = entry * (trail2_after / 100.0)

    peak_pnl = 0.0
    tp_hit = False
    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for ts, o, h, l, c in bars:
            peak_pnl = max(peak_pnl, (highest - entry) / entry * 100.0)
            if tp is not None and h >= tp:
                sl = max(sl, tp); tp = None; tp_hit = True
            eff = trail2_d if (trail2_after_d and highest >= entry + trail2_after_d) else trail1_d
            highest = max(highest, h)
            sl = max(sl, highest - eff)
            if highest >= entry + be:
                sl = max(sl, entry)
            if l <= sl:
                return pnl_of(side, entry, sl), "SL", peak_pnl, tp_hit
        return pnl_of(side, entry, bars[-1][4] if bars else entry), "EOD", peak_pnl, tp_hit
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for ts, o, h, l, c in bars:
            peak_pnl = max(peak_pnl, (entry - lowest) / entry * 100.0)
            if tp is not None and l <= tp:
                sl = min(sl, tp); tp = None; tp_hit = True
            eff = trail2_d if (trail2_after_d and lowest <= entry - trail2_after_d) else trail1_d
            lowest = min(lowest, l)
            sl = min(sl, lowest + eff)
            if lowest <= entry - be:
                sl = min(sl, entry)
            if h >= sl:
                return pnl_of(side, entry, sl), "SL", peak_pnl, tp_hit
        return pnl_of(side, entry, bars[-1][4] if bars else entry), "EOD", peak_pnl, tp_hit


def pnl_of(side, entry, exit_px):
    gross = (exit_px - entry) / entry * 100.0 if side == "long" else (entry - exit_px) / entry * 100.0
    return gross - 100.0 * 0.05 / 100 * 2


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    sl_tf = e.get("sl_timeframe", "1h")
    split_ratio = float(e.get("paper", {}).get("entry_split_ratio", 0.5))
    tp_usd = float(e.get("fixed_tp_usd", 2.04))

    trades = bo.load_trades()
    bars1m = bo.load_bars()
    ex = bs.build_exchange(cfg)
    syms = sorted({t["symbol"] for t in trades})
    tf_bars = {}
    for sym in syms:
        ts = [t for t in trades if t["symbol"] == sym]
        since = int(min(t["opened_at"] for t in ts) * 1000) - 7 * 24 * 3600_000
        until = int(time.time() * 1000)
        tf_bars[sym] = fetch_tf_bars(ex, sym, sl_tf, since, until)

    prepared = []
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in bars1m.get(sym, []) if ts >= opened_ms]
        pre = [b for b in tf_bars.get(sym, []) if b[0] < opened_ms][-15:]
        atr = bo.atr_from_bars(pre)
        avg = after[0][1] * split_ratio + after[1][4] * (1 - split_ratio) if len(after) >= 2 else entry
        prepared.append({"side": side, "entry": avg, "atr": atr, "bars": after})

    # 当前参数
    results = []
    for p in prepared:
        pnl, reason, peak, tp_hit = simulate_detail(p, tp_usd, 2.53, 1.02, 1.71, 4.82, 2.83, 0.61, 7.84, 0.53)
        results.append({"pnl": pnl, "reason": reason, "peak": peak, "tp_hit": tp_hit})

    n = len(results)
    tp_hit_n = sum(1 for r in results if r["tp_hit"])
    wins = [r for r in results if r["pnl"] > 0]
    # 锁利后回吐分析：触发了锁利但最终平仓 pnl < 峰值 pnl 的比例
    giveback = [r for r in results if r["tp_hit"] and r["pnl"] < r["peak"]]
    # 峰值分布（锁利触发的单）
    peaks_tp = [r["peak"] for r in results if r["tp_hit"]]

    print(f"止盈链路诊断（锁利门槛 {tp_usd}u，移动止损 2.83→0.61u@7.84u）\n")
    print(f"总笔数 {n}")
    print(f"触及锁利门槛(≥{tp_usd}u) : {tp_hit_n} 笔 ({tp_hit_n/n*100:.1f}%)")
    print(f"未触及锁利(直接止损)     : {n-tp_hit_n} 笔 ({(n-tp_hit_n)/n*100:.1f}%)\n")

    if peaks_tp:
        print(f"锁利单的峰值浮盈分布：")
        peaks_tp.sort()
        def q(x): return peaks_tp[int(len(peaks_tp)*x)]
        print(f"  中位数 {q(0.5):.2f}u / 75%分位 {q(0.75):.2f}u / 90%分位 {q(0.9):.2f}u / 最大 {max(peaks_tp):.2f}u")
        # 有多少单峰值超过了收紧触发线 7.84u
        over_tighten = sum(1 for x in peaks_tp if x >= 7.84)
        print(f"  峰值超过收紧触发线 7.84u 的单: {over_tighten} 笔 ({over_tighten/len(peaks_tp)*100:.1f}%)")

    print(f"\n锁利后利润回吐（tp_hit 但最终 pnl < 峰值）：")
    if tp_hit_n:
        giveback_avg = sum((r["peak"] - r["pnl"]) for r in giveback) / len(giveback) if giveback else 0
        print(f"  回吐笔数 {len(giveback)}/{tp_hit_n} ({len(giveback)/tp_hit_n*100:.1f}%)")
        print(f"  平均回吐 {giveback_avg:.2f}u")

    print(f"\n结论：")
    if tp_hit_n / n < 0.4:
        print(f"  ⚠️ 只有 {tp_hit_n/n*100:.0f}% 的单能触及锁利门槛，锁利门槛 {tp_usd}u 可能偏高")
    if peaks_tp and max(peaks_tp) < 7.84:
        print(f"  ⚠️ 没有任何单峰值超过 7.84u，收紧档(trail2=0.61u)从未触发，等于只有基础档")
    elif over_tighten / len(peaks_tp) < 0.1 if peaks_tp else False:
        print(f"  ⚠️ 收紧档极少触发，trail_step2 参数形同虚设")


if __name__ == "__main__":
    main()
