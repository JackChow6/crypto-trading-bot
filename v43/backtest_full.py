#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_full.py —— 完整回测：优化后入场 + 当前寻优止盈止损。

对每笔实盘成交：
  1. 入场模拟：split 分批（一半市价 + 一半限价追踪建仓），得平均入场价
  2. 止盈止损模拟：用平均入场价走当前寻优参数（动态止损 ATR + 动态止盈锁利 + 分级移动止损）
  3. 汇总对比三组：
     A. 实盘实际（旧参数 + 纯市价）         —— 真实历史 pnl
     B. 纯市价 + 当前止盈止损               —— 入场不变，只换止盈止损
     C. split 追踪入场 + 当前止盈止损       —— 完整优化

K 线复用 backtest_bars_cache.json（auto_optimize 已刷新到最新）。
"""
from __future__ import annotations

import json
import os
import sys

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import backtest_optuna as bo

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")


def simulate_entry(side, bars, ratio, offset_pct, chase_range_pct, chase_step_pct, timeout_sec):
    """模拟 split 分批入场，返回 (avg_entry, limit_filled)。

    市价半仓 = 第一根 K 线 open 价；限价半仓 = offset 下方挂单 + 追踪建仓追价。
    """
    if not bars:
        return None, False
    market_px = bars[0][1]   # 第一根 open 作为市价成交价
    open_ts = bars[0][0]
    timeout_ms = timeout_sec * 1000

    if side == "long":
        limit_px = market_px * (1 - offset_pct / 100)
        floor = market_px * (1 - chase_range_pct / 100)
    else:
        limit_px = market_px * (1 + offset_pct / 100)
        cap = market_px * (1 + chase_range_pct / 100)

    limit_fill = None
    for ts, o, h, l, c in bars:
        if ts - open_ts > timeout_ms:
            break
        if side == "long":
            if l <= limit_px:          # 价格触及挂单价，成交
                limit_fill = limit_px
                break
            # 未成交：追踪建仓，追挂更低（用 close 作为新盘口参考）
            new_px = max(floor, c)
            if new_px < limit_px and (limit_px - new_px) / limit_px >= chase_step_pct / 100:
                limit_px = new_px
        else:
            if h >= limit_px:
                limit_fill = limit_px
                break
            new_px = min(cap, c)
            if new_px > limit_px and (new_px - limit_px) / limit_px >= chase_step_pct / 100:
                limit_px = new_px

    if limit_fill is not None:
        avg = market_px * ratio + limit_fill * (1 - ratio)
        return avg, True
    return market_px, False   # 限价未成交，只剩市价半仓


def simulate_entry_maker(side, bars, ratio, timeout_sec):
    """模拟 maker 挂单入场：限价半仓挂盘口买一/卖一价（≈第一根 K 线 open）。

    maker 单贴盘口，几乎下一根 K 线即成交（价格只要不单边远离就能吃进）。
    近似：限价单以 open 价挂出，下一根 K 线收盘价成交（贴盘口、快速成交）。
    返回 (avg_entry, limit_filled)。
    """
    if not bars or len(bars) < 1:
        return None, False
    market_px = bars[0][1]
    if len(bars) < 2:
        return market_px, False
    # maker 单以盘口最优价（≈open）挂出，下一根 K 线成交
    limit_fill = bars[1][4]   # 下一根 close 作为成交价
    avg = market_px * ratio + limit_fill * (1 - ratio)
    return avg, True


def pnl_entry_adjust(side, entry, avg_entry, limit_filled, ratio):
    """入场改善：做多均价更低为正、做空均价更高为正，返回改善百分比。"""
    if not entry:
        return 0.0
    if side == "long":
        return (entry - avg_entry) / entry * 100
    return (avg_entry - entry) / entry * 100


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    e = cfg["engine"]
    paper = e.get("paper", {})

    # 止盈止损参数（当前寻优结果）
    tp_usd = float(e.get("fixed_tp_usd", 2.5))
    sl_base = float(e.get("fixed_sl_usd", 1.1))
    sl_atr_mult = float(e.get("dyn_sl_atr_mult", 3.7))
    sl_min = float(e.get("sl_min_usd", 1.4))
    sl_max = float(e.get("sl_max_usd", 6.6))
    trail1 = float(e.get("trail_step1_usd", 2.8))
    trail2 = float(e.get("trail_step2_usd", 0.4))
    trail2_after = float(e.get("trail_step2_after_usd", 5.0))
    be_ratio = float(e.get("break_even_ratio", 0.9))

    # 入场参数
    split_ratio = float(paper.get("entry_split_ratio", 0.5))
    offset_pct = float(paper.get("entry_limit_offset_pct", 0.3))
    chase_range = float(paper.get("entry_chase_range_pct", 1.0))
    chase_step = float(paper.get("entry_chase_min_step_pct", 0.05))
    timeout = float(paper.get("limit_timeout_sec", 600))

    trades = bo.load_trades()
    bars_cache = bo.load_bars()
    prepared = bo.prepare_trades(trades, bars_cache)   # 复用 ATR/bars 计算

    print(f"回测 {len(prepared)} 笔实盘成交")
    print(f"止盈止损参数：锁利{tp_usd}u | 动态止损{sl_min}~{sl_max}u(ATR×{sl_atr_mult}) | "
          f"移动止损{trail1}/{trail2}u@{trail2_after}u | 保本{be_ratio}")
    print(f"入场参数：split市价{int(split_ratio*100)}% + 限价offset{offset_pct}% + 追踪建仓(范围{chase_range}%)\n")

    # 四组结果
    real_pnls = []          # A: 实盘实际
    market_pnls = []        # B: 纯市价 + 当前止盈止损
    split_pnls = []         # C: split追踪入场 + 当前止盈止损
    maker_pnls = []         # D: maker挂单入场 + 当前止盈止损
    entry_improve = []      # 入场价改善%
    limit_filled_cnt = 0
    chase_cnt = 0

    for i, (t, p) in enumerate(zip(trades, prepared)):
        side = p["side"]
        entry = p["entry"]
        atr = p["atr"]
        bars = p["bars"]

        # A: 实盘实际 pnl
        real_pnls.append(t.get("pnl", 0.0))

        # B: 纯市价（实盘 entry）+ 当前止盈止损
        pnl_b = bo.simulate(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                            trail1, trail2, trail2_after, be_ratio)
        market_pnls.append(pnl_b)

        # C: split 入场 + 当前止盈止损
        avg_entry, limit_filled = simulate_entry(side, bars, split_ratio, offset_pct,
                                                 chase_range, chase_step, timeout)
        if avg_entry is None:
            split_pnls.append(pnl_b)
        else:
            if limit_filled:
                limit_filled_cnt += 1
            improve = pnl_entry_adjust(side, entry, avg_entry, limit_filled, split_ratio)
            entry_improve.append(improve)
            p2 = {"side": side, "entry": avg_entry, "atr": atr, "bars": bars}
            pnl_c = bo.simulate(p2, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                trail1, trail2, trail2_after, be_ratio)
            if not limit_filled:
                pnl_c *= split_ratio   # 限价没成交，只剩市价半仓
            split_pnls.append(pnl_c)

        # D: maker 挂单入场 + 当前止盈止损（限价半仓挂盘口价，几乎必成交）
        avg_entry_m, limit_filled_m = simulate_entry_maker(side, bars, split_ratio, timeout)
        if avg_entry_m is None:
            maker_pnls.append(pnl_b)
        else:
            p3 = {"side": side, "entry": avg_entry_m, "atr": atr, "bars": bars}
            pnl_d = bo.simulate(p3, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                trail1, trail2, trail2_after, be_ratio)
            maker_pnls.append(pnl_d)

    def stats(pnls):
        n = len(pnls)
        total = sum(pnls)
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x <= 0]
        wr = len(wins) / n * 100 if n else 0
        pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
        return {"n": n, "total": total, "wr": wr, "pf": pf,
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": sum(losses) / len(losses) if losses else 0}

    a, b, c, d = stats(real_pnls), stats(market_pnls), stats(split_pnls), stats(maker_pnls)

    print("=" * 78)
    print(f"{'方案':<36}{'总盈亏':<12}{'胜率':<10}{'盈亏比':<10}{'平均盈/亏'}")
    print("=" * 78)
    print(f"{'A 实盘实际(旧参数+纯市价)':<32}{a['total']:<+12.2f}{a['wr']:<10.1f}{a['pf']:<10.2f}{a['avg_win']:+.2f}/{a['avg_loss']:.2f}")
    print(f"{'B 纯市价+当前止盈止损':<32}{b['total']:<+12.2f}{b['wr']:<10.1f}{b['pf']:<10.2f}{b['avg_win']:+.2f}/{b['avg_loss']:.2f}")
    print(f"{'C split追踪入场+当前止盈止损':<32}{c['total']:<+12.2f}{c['wr']:<10.1f}{c['pf']:<10.2f}{c['avg_win']:+.2f}/{c['avg_loss']:.2f}")
    print(f"{'D maker挂单入场+当前止盈止损':<32}{d['total']:<+12.2f}{d['wr']:<10.1f}{d['pf']:<10.2f}{d['avg_win']:+.2f}/{d['avg_loss']:.2f}")
    print("=" * 78)

    # 入场改善统计
    if entry_improve:
        avg_imp = sum(entry_improve) / len(entry_improve)
        pos_imp = sum(1 for x in entry_improve if x > 0)
        print(f"\n入场优化统计（split 追踪建仓）：")
        print(f"  限价半仓成交率 = {limit_filled_cnt}/{len(prepared)} ({limit_filled_cnt/len(prepared)*100:.1f}%)")
        print(f"  平均入场价改善 = {avg_imp:+.3f}%（正=成交价更优）")
        print(f"  改善为正的笔数 = {pos_imp}/{len(entry_improve)} ({pos_imp/len(entry_improve)*100:.1f}%)")

    print(f"\n结论：")
    print(f"  止盈止损优化的贡献（B vs A）= {b['total']-a['total']:+.2f} USDT")
    print(f"  split追踪入场贡献（C vs B）= {c['total']-b['total']:+.2f} USDT")
    print(f"  maker挂单入场贡献（D vs B）= {d['total']-b['total']:+.2f} USDT")
    print(f"  最优方案 = {'maker挂单(D)' if d['total']>=max(b['total'],c['total']) else ('纯市价(B)' if b['total']>=c['total'] else 'split追踪(C)')}"
          f" 总盈亏 {max(a['total'],b['total'],c['total'],d['total']):+.2f} USDT")


if __name__ == "__main__":
    main()
