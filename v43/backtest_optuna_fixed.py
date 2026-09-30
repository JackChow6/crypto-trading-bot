#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_optuna_fixed.py —— 固定止盈模式的 optuna 寻优。

固定止盈：触及止盈价立即平仓、触及止损价立即平仓，不做动态锁利/移动止损/保本。
只寻优真正起作用的 5 个参数：
  - tp_usd       固定止盈金额
  - sl_base_usd  止损基准（ATR 缺失回退）
  - sl_atr_mult  动态止损倍数（1h ATR）
  - sl_min/max   动态止损区间

止损 ATR 用 1h 周期（与引擎 _sl_atr 一致）。
"""
from __future__ import annotations
import json, os, sys, time
import optuna
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import backtest_optuna as bo
import backtest_scan as bs

CONFIG = os.path.join(ROOT, "config.yaml")
NOTIONAL = 100.0
FEE_PCT = 0.05


def simulate_fixed(prep, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                   roi_mid_usd=None, roi_min_usd=None, mid_min=60, min_min=180,
                   sl_decay_mid=None, sl_decay_min=None, sl_mid_min=120, sl_min_min=240):
    """固定止盈止损 + 时间衰减（止盈门槛降低 + 止损收紧）。

    时间衰减止盈（roi_mid_usd 非 None）：持仓分钟数越多，止盈门槛越低。
    时间衰减止损（sl_decay_mid 非 None）：持仓分钟数越多，止损越紧（sl 向 entry 靠拢）。
    """
    side = prep["side"]; entry = prep["entry"]; atr = prep["atr"]; bars = prep["bars"]
    # 原始止损金额（1h ATR 动态）
    base_sl_usd = sl_base
    if atr and entry:
        base_sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        base_sl_usd = max(sl_min, min(sl_max, base_sl_usd))

    def cur_sl_usd(minutes):
        """按持仓分钟数返回当前止损金额（时间衰减收紧）。"""
        if sl_decay_mid is None:
            return base_sl_usd
        if minutes <= sl_mid_min:
            return base_sl_usd
        elif minutes <= sl_min_min:
            return sl_decay_mid
        else:
            return sl_decay_min

    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            # 当前止损价（随时间衰减收紧）
            sl = entry - entry * (cur_sl_usd(minutes) / NOTIONAL)
            # 当前止盈价（随时间衰减降低）
            if roi_mid_usd is not None:
                if minutes <= mid_min:
                    roi = tp_usd
                elif minutes <= min_min:
                    roi = roi_mid_usd
                else:
                    roi = roi_min_usd
                tp = entry + entry * (roi / NOTIONAL)
            else:
                tp = entry + entry * (tp_usd / NOTIONAL)
            if l <= sl:
                return bo.pnl_of(side, entry, sl)
            if h >= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            minutes = i + 1
            sl = entry + entry * (cur_sl_usd(minutes) / NOTIONAL)
            if roi_mid_usd is not None:
                if minutes <= mid_min:
                    roi = tp_usd
                elif minutes <= min_min:
                    roi = roi_mid_usd
                else:
                    roi = roi_min_usd
                tp = entry - entry * (roi / NOTIONAL)
            else:
                tp = entry - entry * (tp_usd / NOTIONAL)
            if h >= sl:
                return bo.pnl_of(side, entry, sl)
            if l <= tp:
                return bo.pnl_of(side, entry, tp)
        return bo.pnl_of(side, entry, bars[-1][4] if bars else entry)


def objective(trial, prepared):
    tp_usd = trial.suggest_float("tp_usd", 0.5, 5.0)
    sl_base = trial.suggest_float("sl_base_usd", 0.5, 5.0)
    sl_atr_mult = trial.suggest_float("sl_atr_mult", 0.3, 2.0)
    sl_min = trial.suggest_float("sl_min_usd", 0.3, 3.0)
    sl_max = trial.suggest_float("sl_max_usd", 3.0, 8.0)
    # 止盈时间衰减参数
    roi_mid_usd = trial.suggest_float("roi_mid_usd", 0.3, tp_usd)
    roi_min_usd = trial.suggest_float("roi_min_usd", 0.1, 1.0)
    mid_min = trial.suggest_int("mid_min", 30, 120)
    min_min = trial.suggest_int("min_min", 120, 360)
    # 止损时间衰减参数
    sl_decay_mid = trial.suggest_float("sl_decay_mid", 0.5, 2.0)
    sl_decay_min = trial.suggest_float("sl_decay_min", 0.2, 1.0)
    sl_mid_min = trial.suggest_int("sl_mid_min", 60, 180)
    sl_min_min = trial.suggest_int("sl_min_min", 180, 480)
    if sl_min >= sl_max:
        raise optuna.TrialPruned()
    if roi_mid_usd >= tp_usd:
        raise optuna.TrialPruned()
    if roi_min_usd >= roi_mid_usd:
        raise optuna.TrialPruned()
    if mid_min >= min_min:
        raise optuna.TrialPruned()
    if sl_decay_min >= sl_decay_mid:
        raise optuna.TrialPruned()
    if sl_mid_min >= sl_min_min:
        raise optuna.TrialPruned()
    total = 0.0
    for p in prepared:
        total += simulate_fixed(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                                roi_mid_usd, roi_min_usd, mid_min, min_min,
                                sl_decay_mid, sl_decay_min, sl_mid_min, sl_min_min)
    return total


def main():
    trades = bo.load_trades()
    bars_cache = bo.load_bars()
    sl_bars = bo.load_sl_bars()
    prepared = bo.prepare_trades(trades, bars_cache, sl_bars)
    print(f"固定止盈寻优：{len(prepared)} 笔，ATR 可用 {sum(1 for p in prepared if p['atr'])} 笔\n")

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(lambda t: objective(t, prepared), n_trials=600, show_progress_bar=True)

    best = study.best_params
    print("\n" + "=" * 60)
    print("固定止盈+止盈/止损双时间衰减寻优：最优参数")
    print("=" * 60)
    print(f"止盈金额 tp_usd        = {best['tp_usd']:.2f}")
    print(f"止损基准 sl_base_usd   = {best['sl_base_usd']:.2f}")
    print(f"动态止损倍数 sl_atr_mult = {best['sl_atr_mult']:.2f}")
    print(f"止损下限 sl_min_usd    = {best['sl_min_usd']:.2f}")
    print(f"止损上限 sl_max_usd    = {best['sl_max_usd']:.2f}")
    print(f"止盈衰减中档 roi_mid_usd = {best['roi_mid_usd']:.2f} @ {best['mid_min']}min")
    print(f"止盈衰减低档 roi_min_usd = {best['roi_min_usd']:.2f} @ {best['min_min']}min")
    print(f"止损衰减中档 sl_decay_mid = {best['sl_decay_mid']:.2f} @ {best['sl_mid_min']}min")
    print(f"止损衰减低档 sl_decay_min = {best['sl_decay_min']:.2f} @ {best['sl_min_min']}min")
    print(f"\n最优总盈亏 = {study.best_value:+.2f} USDT")

    # 胜率/盈亏比
    pnls = [simulate_fixed(p, best['tp_usd'], best['sl_base_usd'], best['sl_atr_mult'],
                           best['sl_min_usd'], best['sl_max_usd'],
                           best['roi_mid_usd'], best['roi_min_usd'],
                           best['mid_min'], best['min_min'],
                           best['sl_decay_mid'], best['sl_decay_min'],
                           best['sl_mid_min'], best['sl_min_min']) for p in prepared]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    wr = len(wins) / len(pnls) * 100
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    print(f"胜率 = {wr:.1f}% | 盈亏比 = {pf:.2f}")
    print("=" * 60)

    top = sorted(study.trials, key=lambda t: (t.value or -1e9), reverse=True)[:10]
    print("\nTop 10：")
    for i, t in enumerate(top):
        p = t.params
        print(f"{i+1:2d}. {t.value:+.2f}  tp={p['tp_usd']:.1f} sl_base={p['sl_base_usd']:.1f} "
              f"atr_mult={p['sl_atr_mult']:.2f} sl[{p['sl_min_usd']:.1f}~{p['sl_max_usd']:.1f}]")


if __name__ == "__main__":
    main()
