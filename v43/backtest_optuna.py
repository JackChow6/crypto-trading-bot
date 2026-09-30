#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_optuna.py —— 用 optuna 贝叶斯寻优，找实盘记录区间内的最优止盈止损参数。

完整复刻引擎当前逻辑：
  - 动态止损：sl_usd = clamp(atr × sl_atr_mult × notional / entry, sl_min_usd, sl_max_usd)
  - 锁利：浮盈达 fixed_tp_usd → SL 移到止盈位，清空 TP，继续奔跑
  - 分级移动止损：回撤容忍 trail_step1_usd，浮盈超 trail_step2_after_usd 后收紧到 trail_step2_usd
  - 保本：浮盈超 break_even_ratio × sl_dist 后止损移到成本价

关键：ATR 用「止损周期」（sl_timeframe，默认 1h）计算，与引擎 _sl_atr() 完全一致，
保证寻优出的参数就是引擎实际运行时的最优参数。
目标函数 = 总净盈亏（含手续费），TPE 采样器搜索。
"""
from __future__ import annotations

import json
import os
import sys
import time

import optuna
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")          # 1m K线（模拟路径用）
CACHE_SL = os.path.join(BASE, "backtest_sl_bars_cache.json")    # 止损周期 K线（ATR 用）

NOTIONAL = 100.0
FEE_PCT = 0.05   # 单边 0.05%，开平各一次


def load_trades():
    with open(STATE, "r", encoding="utf-8") as f:
        return json.load(f).get("trades", [])


def load_bars():
    with open(CACHE, "r", encoding="utf-8") as f:
        return json.load(f).get("bars", {})


def load_sl_bars():
    try:
        with open(CACHE_SL, "r", encoding="utf-8") as f:
            return json.load(f).get("bars", {})
    except Exception:
        return {}


def atr_from_bars(bars):
    """bars: [(ts,o,h,l,c), ...]，计算 ATR(14)。"""
    if len(bars) < 15:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    v = sum(trs[:14]) / 14
    for t in trs[14:]:
        v = (v * 13 + t) / 14
    return v


def fetch_tf_bars(ex, sym, timeframe, since_ms, until_ms):
    """拉指定周期 K 线，返回 [[ts,o,h,l,c], ...] 按时间升序。"""
    out = []
    cursor = since_ms
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
            "4h": 14_400_000, "1d": 86_400_000}.get(timeframe, 3_600_000)
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


def prepare_trades(trades, bars_cache, sl_bars=None):
    """预处理：每笔交易 → (side, entry, atr_at_open, bars_after_open)。

    atr 优先用止损周期 K 线（sl_bars，与引擎 _sl_atr 一致），缺失回退 1m。
    bars（模拟路径）用 1m K 线。
    """
    prepared = []
    for t in trades:
        sym = t["symbol"]
        side = t["side"]
        entry = t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        allbars = bars_cache.get(sym, [])
        # 止损周期 ATR（优先）
        atr = None
        if sl_bars:
            pre_sl = [b for b in sl_bars.get(sym, []) if b[0] < opened_ms][-15:]
            atr = atr_from_bars(pre_sl)
        if atr is None:
            pre = [b for b in allbars if b[0] < opened_ms][-15:]
            atr = atr_from_bars(pre)
        # 开仓后 K 线（模拟路径）
        after = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
        prepared.append({"side": side, "entry": entry, "atr": atr, "bars": after})
    return prepared


def simulate(prep, tp_usd, sl_base_usd, sl_atr_mult, sl_min_usd, sl_max_usd,
             trail1_usd, trail2_usd, trail2_after_usd, be_ratio):
    """复刻引擎动态止盈止损逻辑，返回净盈亏。"""
    side = prep["side"]
    entry = prep["entry"]
    atr = prep["atr"]
    bars = prep["bars"]

    # 动态止损金额
    sl_usd = sl_base_usd
    if atr and entry:
        sl_usd = atr * sl_atr_mult * NOTIONAL / entry
        sl_usd = max(sl_min_usd, min(sl_max_usd, sl_usd))
    sl_dist = entry * (sl_usd / NOTIONAL)
    tp_dist = entry * (tp_usd / NOTIONAL)
    be = sl_dist * be_ratio
    trail1 = entry * (trail1_usd / NOTIONAL)
    trail2 = entry * (trail2_usd / NOTIONAL)
    trail2_after = entry * (trail2_after_usd / NOTIONAL)

    if side == "long":
        tp, sl = entry + tp_dist, entry - sl_dist
        highest = entry
        for ts, o, h, l, c in bars:
            # 锁利
            if tp is not None and h >= tp:
                sl = max(sl, tp)
                tp = None
            # 分级移动止损
            eff = trail2 if (trail2_after and highest >= entry + trail2_after) else trail1
            highest = max(highest, h)
            sl = max(sl, highest - eff)
            # 保本
            if highest >= entry + be:
                sl = max(sl, entry)
            if l <= sl:
                return pnl_of("long", entry, sl)
            if c <= sl:
                return pnl_of("long", entry, sl)
        return pnl_of("long", entry, bars[-1][4] if bars else entry)
    else:
        tp, sl = entry - tp_dist, entry + sl_dist
        lowest = entry
        for ts, o, h, l, c in bars:
            if tp is not None and l <= tp:
                sl = min(sl, tp)
                tp = None
            eff = trail2 if (trail2_after and lowest <= entry - trail2_after) else trail1
            lowest = min(lowest, l)
            sl = min(sl, lowest + eff)
            if lowest <= entry - be:
                sl = min(sl, entry)
            if h >= sl:
                return pnl_of("short", entry, sl)
            if c >= sl:
                return pnl_of("short", entry, sl)
        return pnl_of("short", entry, bars[-1][4] if bars else entry)


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def objective(trial, prepared):
    tp_usd = trial.suggest_float("tp_usd", 0.5, 5.0)
    sl_base = trial.suggest_float("sl_base_usd", 1.0, 6.0)
    # sl_atr_mult 针对「止损周期(1h) ATR」量级：1h ATR 较大，倍数应偏小（0.3~1.5）
    # 否则 1h_ATR × 大倍数 会普遍撞 sl_max_usd 上限，动态止损退化成固定上限
    sl_atr_mult = trial.suggest_float("sl_atr_mult", 0.3, 1.5)
    sl_min = trial.suggest_float("sl_min_usd", 0.5, 3.0)
    sl_max = trial.suggest_float("sl_max_usd", 3.0, 9.0)
    trail1 = trial.suggest_float("trail1_usd", 0.5, 4.0)
    trail2 = trial.suggest_float("trail2_usd", 0.3, 2.0)
    trail2_after = trial.suggest_float("trail2_after_usd", 2.0, 8.0)
    be_ratio = 1.0   # 固定 1.0：保本门槛=止损距离，与锁利门槛对齐，避免"保本后未锁利就反弹归零"

    # 约束：收紧档回撤 < 基础档；收紧触发线 > 锁利门槛
    if trail2 >= trail1:
        raise optuna.TrialPruned()
    if trail2_after <= tp_usd:
        raise optuna.TrialPruned()
    # 关键约束：移动止损回撤容忍必须 < 锁利门槛，
    # 否则「最低/最高点 + 回撤容忍」会越过入场价，止损跑到亏损区（止盈止损方向反了）
    if trail1 >= tp_usd:
        raise optuna.TrialPruned()

    total = 0.0
    for p in prepared:
        total += simulate(p, tp_usd, sl_base, sl_atr_mult, sl_min, sl_max,
                          trail1, trail2, trail2_after, be_ratio)
    return total


def main():
    trades = load_trades()
    bars_cache = load_bars()
    sl_bars = load_sl_bars()
    if sl_bars:
        print(f"使用止损周期 K 线缓存（{len(sl_bars)} 币种）")
    prepared = prepare_trades(trades, bars_cache, sl_bars)
    print(f"预处理完成：{len(prepared)} 笔交易，ATR 可用 {sum(1 for p in prepared if p['atr'])} 笔\n")

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(lambda t: objective(t, prepared), n_trials=500, show_progress_bar=True)

    print("\n" + "=" * 70)
    print("寻优完成：最优参数（止损周期 ATR 口径）")
    print("=" * 70)
    best = study.best_params
    print(f"止盈锁利门槛 tp_usd         = {best['tp_usd']:.2f}")
    print(f"止损基准 sl_base_usd        = {best['sl_base_usd']:.2f}")
    print(f"动态止损倍数 sl_atr_mult     = {best['sl_atr_mult']:.2f}")
    print(f"止损下限 sl_min_usd         = {best['sl_min_usd']:.2f}")
    print(f"止损上限 sl_max_usd         = {best['sl_max_usd']:.2f}")
    print(f"移动止损基础 trail1_usd     = {best['trail1_usd']:.2f}")
    print(f"移动止损收紧 trail2_usd     = {best['trail2_usd']:.2f}")
    print(f"收紧触发线 trail2_after_usd = {best['trail2_after_usd']:.2f}")
    print(f"保本比例 be_ratio           = {best['be_ratio']:.2f}")
    print(f"\n最优总净盈亏 = {study.best_value:+.2f} USDT")
    print("=" * 70)

    top = sorted(study.trials, key=lambda t: (t.value or -1e9), reverse=True)[:15]
    print("\nTop 15 参数组合：")
    for i, t in enumerate(top):
        p = t.params
        print(f"{i+1:2d}. 盈亏{t.value:+.2f}  tp={p['tp_usd']:.1f} sl_atr_mult={p['sl_atr_mult']:.2f} "
              f"sl[{p['sl_min_usd']:.1f}~{p['sl_max_usd']:.1f}] trail={p['trail1_usd']:.1f}/{p['trail2_usd']:.1f}@"
              f"{p['trail2_after_usd']:.1f} be={p['be_ratio']:.1f}")


if __name__ == "__main__":
    main()

