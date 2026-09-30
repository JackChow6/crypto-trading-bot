#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_signals_opt.py —— 基于「产生的信号」的寻优 objective。

数据源：decision_log.jsonl 的决策信号（含被拒的），按时间顺序重放 +
仓位上限约束，拉真实 K 线模拟盈亏。

与 backtest_optuna_fixed 的区别：
  - 后者用实盘成交记录（live_state.json 的 trades，只含实际成交的）
  - 本模块用「全部产生的信号」（含被拒的 235 个），更真实反映机会成本
"""
from __future__ import annotations
import json, os, sys, time
import optuna

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

DECISION_LOG = os.path.join(BASE, "decision_log.jsonl")
NOTIONAL = 100.0
FEE_PCT = 0.05


def load_signals(hours=None):
    """读 decision_log，返回按时间排序的信号列表。hours 非 None 时只取最近 N 小时。"""
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
    if hours is not None:
        cutoff = time.time() - hours * 3600
        decs = [d for d in decs if d.get("ts", 0) >= cutoff]
    return decs


def pnl_of(side, entry, exit_px):
    if side == "long":
        gross = (exit_px - entry) / entry * NOTIONAL
    else:
        gross = (entry - exit_px) / entry * NOTIONAL
    return gross - NOTIONAL * FEE_PCT / 100 * 2


def simulate(side, entry, atr, bars, params):
    """按固定止盈 + 止盈/止损双时间衰减模拟，返回 (pnl, exit_idx)。

    止损用 ATR 动态：sl_usd = clamp(atr × sl_atr_mult × NOTIONAL / entry, sl_min, sl_max)。
    """
    tp_usd = params["tp"]
    sl_base = params["sl"]
    sl_atr_mult = params["sl_atr_mult"]; sl_min = params["sl_min"]; sl_max = params["sl_max"]
    roi_mid = params["roi_mid"]; roi_min = params["roi_min"]
    mid_min = params["mid_min"]; min_min = params["min_min"]
    sl_decay_mid = params["sl_decay_mid"]; sl_decay_min = params["sl_decay_min"]
    sl_mid_min = params["sl_mid_min"]; sl_min_min = params["sl_min_min"]

    # 基础止损金额（ATR 动态，与引擎 _dynamic_sl_usd 一致）
    base_sl = sl_base
    if atr and entry:
        base_sl = atr * sl_atr_mult * NOTIONAL / entry
        base_sl = max(sl_min, min(sl_max, base_sl))

    def cur_sl(m):
        if sl_decay_mid is None:
            return base_sl
        if m <= sl_mid_min: return base_sl
        elif m <= sl_min_min: return sl_decay_mid
        return sl_decay_min

    def cur_tp(m):
        if roi_mid is None:
            return tp_usd
        if m <= mid_min: return tp_usd
        elif m <= min_min: return roi_mid
        return roi_min

    if side == "long":
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry - entry * (cur_sl(m) / NOTIONAL)
            tp = entry + entry * (cur_tp(m) / NOTIONAL)
            if l <= sl: return pnl_of(side, entry, sl), i
            if h >= tp: return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)
    else:
        for i, (ts, o, h, l, c) in enumerate(bars):
            m = i + 1
            sl = entry + entry * (cur_sl(m) / NOTIONAL)
            tp = entry - entry * (cur_tp(m) / NOTIONAL)
            if h >= sl: return pnl_of(side, entry, sl), i
            if l <= tp: return pnl_of(side, entry, tp), i
        return pnl_of(side, entry, bars[-1][4] if bars else entry), len(bars)


def fetch_bars_after(ex, sym, ts_ms, limit=600):
    try:
        bars = ex.fetch_ohlcv(sym, "1m", since=ts_ms - 60_000, limit=limit)
        return [(b[0], b[1], b[2], b[3], b[4]) for b in bars if b[0] >= ts_ms]
    except Exception:
        return []


def atr_from_bars(bars):
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


def prepare_signals(ex, signals, sl_timeframe="1h"):
    """预加载所有信号的 K 线 + 止损周期 ATR，返回 [{side, entry, ts_ms, atr, bars}, ...]。"""
    prepared = []
    for d in signals:
        sym = d.get("symbol"); action = d.get("action"); entry = d.get("entry"); ts = d.get("ts", 0)
        if not sym or not entry or action not in ("LONG", "SHORT"):
            continue
        ts_ms = int(ts * 1000)
        bars = fetch_bars_after(ex, sym, ts_ms)
        if not bars:
            continue
        # 止损周期 ATR（信号时间之前的 1h K 线）
        atr = None
        try:
            sl_bars = ex.fetch_ohlcv(sym, sl_timeframe, since=ts_ms - 7 * 24 * 3600_000, limit=200)
            pre = [b for b in sl_bars if b[0] < ts_ms][-15:]
            atr = atr_from_bars(pre)
        except Exception:
            atr = None
        prepared.append({"side": action.lower(), "entry": entry, "ts_ms": ts_ms, "atr": atr, "bars": bars})
    prepared.sort(key=lambda p: p["ts_ms"])
    return prepared


def make_objective(prepared, max_slots=15):
    """返回 objective(trial)，带仓位上限约束按时间重放。

    止损用 ATR 动态计算（与引擎 _dynamic_sl_usd 一致）：
      sl_usd = clamp(atr × sl_atr_mult × NOTIONAL / entry, sl_min, sl_max)
    """
    def objective(trial):
        tp_usd = trial.suggest_float("tp_usd", 0.5, 3.5)
        sl_base = trial.suggest_float("sl_base_usd", 0.5, 4.0)
        sl_atr_mult = trial.suggest_float("sl_atr_mult", 0.2, 1.5)
        sl_min = trial.suggest_float("sl_min_usd", 0.3, 2.0)
        sl_max = trial.suggest_float("sl_max_usd", 2.0, 6.0)
        roi_mid_usd = trial.suggest_float("roi_mid_usd", 0.3, tp_usd)
        roi_min_usd = trial.suggest_float("roi_min_usd", 0.1, 1.0)
        mid_min = trial.suggest_int("mid_min", 30, 180)
        min_min = trial.suggest_int("min_min", 120, 480)
        sl_decay_mid = trial.suggest_float("sl_decay_mid", 0.5, 3.0)
        sl_decay_min = trial.suggest_float("sl_decay_min", 0.2, 1.0)
        sl_mid_min = trial.suggest_int("sl_mid_min", 60, 240)
        sl_min_min = trial.suggest_int("sl_min_min", 180, 600)
        # 约束
        if sl_min >= sl_max: raise optuna.TrialPruned()
        if roi_mid_usd >= tp_usd: raise optuna.TrialPruned()
        if roi_min_usd >= roi_mid_usd: raise optuna.TrialPruned()
        if mid_min >= min_min: raise optuna.TrialPruned()
        if sl_decay_min >= sl_decay_mid: raise optuna.TrialPruned()
        if sl_mid_min >= sl_min_min: raise optuna.TrialPruned()

        params = {
            "tp": tp_usd, "sl": sl_base,
            "sl_atr_mult": sl_atr_mult, "sl_min": sl_min, "sl_max": sl_max,
            "roi_mid": roi_mid_usd, "roi_min": roi_min_usd,
            "mid_min": mid_min, "min_min": min_min,
            "sl_decay_mid": sl_decay_mid, "sl_decay_min": sl_decay_min,
            "sl_mid_min": sl_mid_min, "sl_min_min": sl_min_min,
        }
        total = 0.0
        slots = []
        for p in prepared:
            slots = [s for s in slots if s[0] > p["ts_ms"]]
            if max_slots and max_slots > 0 and len(slots) >= max_slots:
                continue
            pnl, exit_idx = simulate(p["side"], p["entry"], p["atr"], p["bars"], params)
            slots.append((p["ts_ms"] + (exit_idx + 1) * 60_000, pnl))
            total += pnl
        return total
    return objective


if __name__ == "__main__":
    import yaml
    import backtest_scan as bs
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    sigs = load_signals(hours=12)
    print(f"加载 {len(sigs)} 个信号")
    prepared = prepare_signals(ex, sigs)
    print(f"有效信号 {len(prepared)} 个")
    obj = make_objective(prepared)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study.optimize(obj, n_trials=300, show_progress_bar=True)
    print(f"\n最优总盈亏 = {study.best_value:+.2f}")
    for k, v in study.best_params.items():
        print(f"  {k} = {v}")
