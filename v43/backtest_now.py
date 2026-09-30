#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_now.py —— 精确回测当前实际运行的完整配置：

  - 入场：split 市价 50% + maker 挂盘口买一/卖一价（返佣）
  - 止损：1h ATR 动态（sl_timeframe=1h，当前配置）
  - 止盈：浮盈 2.55u 锁利 + 分级移动止损 + 保本
  - 参数：全部读 config.yaml 当前值

输出胜率/总盈亏/盈亏比/期望值/最大连续亏损/回撤。
"""
from __future__ import annotations

import json
import os
import sys
import time

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")


def fetch_tf_bars(ex, sym, timeframe, since_ms, until_ms):
    out = []
    cursor = since_ms
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


def simulate_entry_maker(side, bars, ratio):
    """maker 挂单：市价半仓 = 第一根 open，限价半仓 = 下一根 close（贴盘口必成交）。"""
    if not bars or len(bars) < 2:
        if not bars:
            return None
        return bars[0][1], False
    market_px = bars[0][1]
    limit_fill = bars[1][4]
    avg = market_px * ratio + limit_fill * (1 - ratio)
    return avg, True


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    paper = e.get("paper", {})

    # 全部读当前 config
    tp_usd = float(e.get("fixed_tp_usd", 2.55))
    sl_base = float(e.get("fixed_sl_usd", 1.1))
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 3.72))
    sl_min = float(e.get("sl_min_usd", 1.46))
    sl_max = float(e.get("sl_max_usd", 6.61))
    trail1 = float(e.get("trail_step1_usd", 2.78))
    trail2 = float(e.get("trail_step2_usd", 0.44))
    trail2_after = float(e.get("trail_step2_after_usd", 4.98))
    be_ratio = float(e.get("break_even_ratio", 0.97))
    sl_tf = e.get("sl_timeframe", "1h")
    split_ratio = float(paper.get("entry_split_ratio", 0.5))

    trades = bo.load_trades()
    bars1m = bo.load_bars()

    ex = bs.build_exchange(cfg)
    syms = sorted({t["symbol"] for t in trades})
    print(f"拉取 {sl_tf} 止损周期 K 线...")
    tf_bars = {}
    for sym in syms:
        ts = [t for t in trades if t["symbol"] == sym]
        since = int(min(t["opened_at"] for t in ts) * 1000) - 7 * 24 * 3600_000
        until = int(time.time() * 1000)
        tf_bars[sym] = fetch_tf_bars(ex, sym, sl_tf, since, until)
    print("完成\n")

    pnls = []
    wins = 0
    for t in trades:
        sym, side, entry = t["symbol"], t["side"], t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in bars1m.get(sym, []) if ts >= opened_ms]
        # 止损周期 ATR
        pre = [b for b in tf_bars.get(sym, []) if b[0] < opened_ms][-15:]
        atr = bo.atr_from_bars(pre)
        # maker 入场均价
        maker_res = simulate_entry_maker(side, after, split_ratio)
        avg_entry = maker_res[0] if maker_res else entry
        if avg_entry is None:
            avg_entry = entry
        prep = {"side": side, "entry": avg_entry, "atr": atr, "bars": after}
        pnl = bo.simulate(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                          trail1, trail2, trail2_after, be_ratio)
        pnls.append(pnl)
        if pnl > 0:
            wins += 1

    n = len(pnls)
    total = sum(pnls)
    win_list = [x for x in pnls if x > 0]
    loss_list = [x for x in pnls if x <= 0]
    wr = wins / n * 100
    avg_win = sum(win_list) / len(win_list) if win_list else 0
    avg_loss = sum(loss_list) / len(loss_list) if loss_list else 0
    pf = sum(win_list) / abs(sum(loss_list)) if loss_list else float("inf")
    # 最大连续亏损 + 权益曲线最大回撤
    cur = 0.0
    max_dd = 0.0
    peak = 0.0
    eq = 100.0   # 以 100u 初始权益看回撤
    max_eq_dd = 0.0
    for p in pnls:
        cur = cur + p if p < 0 else 0.0
        max_dd = min(max_dd, cur)
        eq += p
        peak = max(peak, eq)
        max_eq_dd = min(max_eq_dd, (eq - peak) / peak * 100)

    print("=" * 60)
    print("当前完整配置回测结果（98 笔实盘成交）")
    print("=" * 60)
    print(f"配置：maker入场 | 止损 {sl_tf} ATR×{sl_atr_mult} | 锁利{tp_usd}u | 移动止损{trail1}/{trail2}u")
    print("-" * 60)
    print(f"总盈亏       : {total:+.2f} USDT")
    print(f"胜率         : {wr:.1f}%  ({wins}/{n})")
    print(f"平均盈利     : {avg_win:+.2f} USDT")
    print(f"平均亏损     : {avg_loss:+.2f} USDT")
    print(f"盈亏比       : {pf:.2f}")
    print(f"期望值/笔    : {total/n:+.3f} USDT")
    print(f"最大连续亏损 : {max_dd:+.2f} USDT")
    print(f"权益最大回撤 : {max_eq_dd:.1f}%")
    print("=" * 60)

    # 与实盘历史对比
    real_total = sum(t.get("pnl", 0.0) for t in trades)
    print(f"\n对比：实盘历史实际总盈亏 = {real_total:+.2f} USDT")
    print(f"      当前配置回测总盈亏 = {total:+.2f} USDT")
    print(f"      提升 = {(total - real_total):+.2f} USDT ({(total/real_total-1)*100 if real_total else 0:+.1f}%)")


if __name__ == "__main__":
    main()
