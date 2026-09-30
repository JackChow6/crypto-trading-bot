#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/auto_optimize.py —— 定时自动寻优止盈止损参数，写回 config 并重启引擎。

流程（循环）：
  1. 停掉当前引擎进程
  2. 刷新 K 线缓存（拉取 live_state.json 里所有交易币种的最新 1m K 线）
  3. optuna 贝叶斯寻优（复用 backtest_optuna 的完整回测逻辑）
  4. 把最优参数写回 config.yaml（保留注释）
  5. 重启引擎，跑 interval 小时
  6. 重复

默认每 6 小时寻优一次，可用 --interval-hours 覆盖，或 --once 只跑一次。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import optuna
import yaml

import backtest_optuna as bo
import backtest_optuna_fixed as bof
import backtest_scan as bs
import backtest_signals_opt as bso

CONFIG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(BASE, "live_state.json")
CACHE = os.path.join(BASE, "backtest_bars_cache.json")
ENGINE_LOG = os.path.join(BASE, "engine.err.log")
PYTHON = sys.executable
ENGINE_CMD = [PYTHON, "-u", "v43/engine.py", "--config", "config.yaml", "--gainers"]

# 寻优参数 → config 键（固定止盈 + 止盈/止损双时间衰减模式）
PARAM_MAP = {
    "tp_usd": "fixed_tp_usd",
    "sl_base_usd": "fixed_sl_usd",
    "sl_atr_mult": "dyn_sl_atr_mult",
    "sl_min_usd": "sl_min_usd",
    "sl_max_usd": "sl_max_usd",
    "roi_mid_usd": "roi_decay_mid_usd",
    "roi_min_usd": "roi_decay_min_usd",
    "mid_min": "roi_decay_mid_min",
    "min_min": "roi_decay_min_min",
    "sl_decay_mid": "sl_decay_mid_usd",
    "sl_decay_min": "sl_decay_min_usd",
    "sl_mid_min": "sl_decay_mid_min",
    "sl_min_min": "sl_decay_min_min",
}

engine_proc = None


def refresh_bars():
    """拉取所有历史交易币种的最新 1m + 止损周期 K 线，写回缓存。

    1m K 线用于模拟价格路径；止损周期 K 线（sl_timeframe）用于计算 ATR，
    与引擎 _sl_atr() 完全一致，保证寻优参数与实盘运行一致。
    """
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    trades = bo.load_trades()
    syms = sorted({t["symbol"] for t in trades})
    sl_tf = cfg.get("engine", {}).get("sl_timeframe", "1h")
    bars = {}
    sl_bars = {}
    print(f"[opt] 刷新 {len(syms)} 个币种的 K 线缓存（1m 路径 + {sl_tf} 止损 ATR）...")
    for s in syms:
        ts = [t for t in trades if t["symbol"] == s]
        since = int(min(t["opened_at"] for t in ts) * 1000)
        until = int(time.time() * 1000)
        bars[s] = bs.fetch_bars(ex, s, since, until)
        # 止损周期 K 线（多拉 7 天用于 ATR(14) 计算）
        sl_since = since - 7 * 24 * 3600_000
        sl_bars[s] = bo.fetch_tf_bars(ex, s, sl_tf, sl_since, until)
        print(f"    {s}: 1m={len(bars[s])} 根, {sl_tf}={len(sl_bars[s])} 根")
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "bars": bars}, f)
    with open(bo.CACHE_SL, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "bars": sl_bars}, f)
    return bars


def run_optuna(trades, bars, n_trials=500):
    """基于「产生的信号」（含被拒的）寻优，带仓位上限约束。"""
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = bs.build_exchange(cfg)
    max_slots = int(cfg.get("engine", {}).get("risk", {}).get("max_open_positions", 0))
    # 0 = 不限制仓位数量，寻优时也取消仓位上限约束
    slot_label = "不限" if max_slots <= 0 else str(max_slots)
    # 加载最近 12 小时的信号（含被拒的），拉真实 K 线
    signals = bso.load_signals(hours=12)
    prepared = bso.prepare_signals(ex, signals)
    print(f"[opt] 加载 {len(signals)} 个信号，有效 {len(prepared)} 个（仓位上限 {slot_label}）")
    if len(prepared) < 20:
        print("[opt] 信号样本不足，跳过寻优保留现有参数")
        return None
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    objective = bso.make_objective(prepared, max_slots=max_slots)
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    print(f"[opt] 寻优完成：最优总盈亏 {study.best_value:+.2f} USDT")
    return study.best_params


def apply_config(params):
    """把寻优结果写回 config.yaml（正则替换，保留中文注释）。"""
    with open(CONFIG, "r", encoding="utf-8") as f:
        text = f.read()
    for opt_key, cfg_key in PARAM_MAP.items():
        raw = params[opt_key]
        # mid_min/min_min 是整数（分钟数），其余保留 2 位小数
        val = int(raw) if isinstance(raw, int) else round(raw, 2)
        # 匹配 "  key: 值" 行，只替换值部分，保留行尾注释。
        # 用 (?<!\S) 和 : 边界锚定，避免 sl_atr_mult 误匹配 sl_min_usd/sl_max_usd 之类子串。
        pat = re.compile(rf"^(\s*{re.escape(cfg_key)}\s*:\s*)([^#\n]*?)(\s*#.*)?$", re.MULTILINE)
        new_text, n = pat.subn(lambda m: f"{m.group(1)}{val}{m.group(3) or ''}", text)
        if n == 1:
            text = new_text
        else:
            print(f"[opt] 警告：config 中 {cfg_key} 未匹配或匹配 {n} 次，跳过")
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(text)
    print("[opt] 已写回 config.yaml")


def start_engine():
    global engine_proc
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(ENGINE_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(ENGINE_CMD, cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT)
    engine_proc = proc
    print(f"[opt] 引擎已启动 PID={proc.pid}")
    return proc


def stop_engine():
    global engine_proc
    if engine_proc is not None:
        try:
            engine_proc.terminate()
            try:
                engine_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                engine_proc.kill()
                engine_proc.wait(timeout=5)
            print(f"[opt] 旧引擎已停止 PID={engine_proc.pid}")
        except Exception as e:
            print(f"[opt] 停引擎出错: {e}")
        engine_proc = None


def once():
    print(f"[opt] {'='*50}")
    print(f"[opt] 手动触发一次寻优 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[opt] {'='*50}")
    stop_engine()
    refresh_bars()
    trades = bo.load_trades()
    bars = bo.load_bars()
    params = run_optuna(trades, bars)
    if params:
        apply_config(params)
        print("[opt] 最优参数：")
        for k, v in params.items():
            print(f"    {k} = {v:.2f}")
    start_engine()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-hours", type=float, default=6.0)
    parser.add_argument("--once", action="store_true", help="只跑一次寻优，不循环")
    args = parser.parse_args()

    if args.once:
        once()
        return

    interval = args.interval_hours * 3600
    print(f"[opt] 自动寻优调度器启动，间隔 {args.interval_hours} 小时")

    # 首次：先启动引擎跑起来（config 已是上次寻优结果）
    start_engine()

    while True:
        # 在 interval 期间，每 30 秒检查引擎是否存活，崩了自动重启
        end = time.time() + interval
        while time.time() < end:
            time.sleep(30)
            if engine_proc is not None and engine_proc.poll() is not None:
                print("[opt] 检测到引擎退出，自动重启")
                start_engine()
        # 到点：停引擎 → 刷新 → 寻优 → 写 config → 重启引擎
        stop_engine()
        try:
            refresh_bars()
        except Exception as e:
            print(f"[opt] 刷新缓存失败: {e}")
        try:
            trades = bo.load_trades()
            bars = bo.load_bars()
            params = run_optuna(trades, bars)
            if params:
                apply_config(params)
        except Exception as e:
            print(f"[opt] 寻优失败: {e}")
        start_engine()


if __name__ == "__main__":
    main()
