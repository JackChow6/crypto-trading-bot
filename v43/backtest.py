#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest.py —— 用实盘交易记录回测「止盈1.5u / 止损1.5u + 动态止盈」策略。

做法：
  1. 读 live_state.json 的 trades（每笔含 symbol/side/entry/opened_at）。
  2. 用币安真实 1m K 线重建每笔开仓后的价格路径（拉到当前，让动态止盈能跑到最近）。
  3. 重放 settle_at 的动态止盈逻辑：
       - 止损 = entry ± 1.5%（金额 1.5u / 名义 100u）
       - 止盈目标 = entry ∓ 1.5%
       - 触及止盈目标 → 锁利：SL 移到止盈位，清空 TP，继续持有（动态止盈核心）
       - 移动止损：保本门槛 0.75%、移动止损距离 0.75%（复刻 settle_at 的 be/trail）
       - 触及 SL → 平仓
  4. 汇总：胜率 / 总盈亏 / 盈亏比 / 期望 / 最大连续亏损。
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

TP_USD = 1.5
SL_USD = 1.5
NOTIONAL = 100.0          # size_usd=5 * leverage=20（与 config risk 一致）
BE_RATIO = 0.5            # 复刻 settle_at：be = sl_dist * 0.5
TRAIL_RATIO = 0.5         # 复刻 settle_at：trail = sl_dist * 0.5
FEE_PCT = 0.05            # 单边吃单手续费 0.05%，开平各一次


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
        d = json.load(f)
    return d.get("trades", [])


def fetch_bars(ex, symbol, since_ms, until_ms, timeframe="1m"):
    """拉取 [since_ms, until_ms] 的 K 线，返回 [(ts, open, high, low, close), ...] 按时间升序。"""
    out = []
    cursor = since_ms
    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        except Exception as e:
            print(f"    拉取 {symbol} K线失败 @{cursor}: {e}")
            break
        if not bars:
            break
        for b in bars:
            ts = b[0]
            if since_ms <= ts <= until_ms:
                out.append((ts, b[1], b[2], b[3], b[4]))
        last_ts = bars[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + 60_000
    return out


def simulate(side, entry, bars, tp_usd=TP_USD, sl_usd=SL_USD, notional=NOTIONAL):
    """重放动态止盈逻辑，返回 (exit_price, exit_reason, bars_held, tp_hit)。

    bars: [(ts, o, h, l, c), ...]
    """
    tp_dist = entry * (tp_usd / notional)
    sl_dist = entry * (sl_usd / notional)
    be = sl_dist * BE_RATIO
    trail = sl_dist * TRAIL_RATIO
    tp_hit = False
    if side == "long":
        tp = entry + tp_dist
        sl = entry - sl_dist
        highest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            # 动态止盈锁利：触及止盈目标 → SL 抬到止盈位，清空 TP，继续奔跑
            if tp is not None and h >= tp:
                sl = max(sl, tp)
                tp = None
                tp_hit = True
            # 移动止损：保本 + 跟随最高点
            highest = max(highest, h)
            if highest >= entry + be:
                sl = max(sl, entry)
                sl = max(sl, highest - trail)
            # 止损判定
            if l <= sl:
                return sl, "SL", i + 1, tp_hit
            if c <= sl:
                return sl, "SL", i + 1, tp_hit
        return bars[-1][4] if bars else entry, "EOD", len(bars), tp_hit
    else:
        tp = entry - tp_dist
        sl = entry + sl_dist
        lowest = entry
        for i, (ts, o, h, l, c) in enumerate(bars):
            if tp is not None and l <= tp:
                sl = min(sl, tp)
                tp = None
                tp_hit = True
            lowest = min(lowest, l)
            if lowest <= entry - be:
                sl = min(sl, entry)
                sl = min(sl, lowest + trail)
            if h >= sl:
                return sl, "SL", i + 1, tp_hit
            if c >= sl:
                return sl, "SL", i + 1, tp_hit
        return bars[-1][4] if bars else entry, "EOD", len(bars), tp_hit


def pnl_of(side, entry, exit_px, notional=NOTIONAL, fee=FEE_PCT):
    """含手续费净盈亏。"""
    if side == "long":
        gross = (exit_px - entry) / entry * notional
    else:
        gross = (entry - exit_px) / entry * notional
    fee_cost = notional * fee / 100 * 2
    return gross - fee_cost


def main():
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    ex = build_exchange(cfg)
    trades = load_trades()
    print(f"共 {len(trades)} 笔已平仓交易，逐笔回测「止盈{TP_USD}u / 止损{SL_USD}u + 动态止盈」\n")

    # 币种去重，一次拉全量 K 线缓存（带时间戳）
    syms = sorted({t["symbol"] for t in trades})
    bars_cache = {}
    for s in syms:
        ts = [t for t in trades if t["symbol"] == s]
        since = int(min(t["opened_at"] for t in ts) * 1000)
        until = int(time.time() * 1000)
        bars_cache[s] = fetch_bars(ex, s, since, until)
        print(f"  拉取 {s}: {len(bars_cache[s])} 根 1m K线")

    results = []
    print()
    for t in trades:
        sym = t["symbol"]
        side = t["side"]
        entry = t["entry"]
        opened_ms = int(t["opened_at"] * 1000)
        allbars = bars_cache.get(sym, [])
        # 只取开仓时间之后的 K 线
        bars = [(ts, o, h, l, c) for (ts, o, h, l, c) in allbars if ts >= opened_ms]
        exit_px, reason, held, tp_hit = simulate(side, entry, bars)
        pnl = pnl_of(side, entry, exit_px)
        # 与真实成交对比
        real_pnl = t.get("pnl", 0.0)
        results.append({
            "symbol": sym, "side": side, "entry": entry,
            "exit": exit_px, "reason": reason, "pnl": round(pnl, 2),
            "real_pnl": real_pnl, "tp_hit": tp_hit, "held": held,
        })
        flag = "锁利" if tp_hit else ("止损" if reason == "SL" else "未触发")
        print(f"  {sym:20s} {side:5s} entry={entry:.8g} exit={exit_px:.8g} "
              f"pnl={pnl:+6.2f} [{flag}] 真实={real_pnl:+6.2f}")

    # 汇总
    n = len(results)
    wins = [r for r in results if r["pnl"] > 0]
    losses = [r for r in results if r["pnl"] <= 0]
    total = sum(r["pnl"] for r in results)
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    win_rate = len(wins) / n * 100 if n else 0
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    # 最大连续亏损
    max_dd = 0
    cur = 0
    for r in results:
        cur = cur + r["pnl"] if r["pnl"] < 0 else 0
        max_dd = min(max_dd, cur)
    tp_hit_cnt = sum(1 for r in results if r["tp_hit"])
    print("\n" + "=" * 60)
    print(f"回测结果：止盈{TP_USD}u / 止损{SL_USD}u + 动态止盈")
    print("=" * 60)
    print(f"总笔数       : {n}")
    print(f"盈利笔数     : {len(wins)} ({win_rate:.1f}%)")
    print(f"亏损笔数     : {len(losses)} ({100-win_rate:.1f}%)")
    print(f"触发动态锁利 : {tp_hit_cnt} 笔 ({tp_hit_cnt/n*100:.1f}%)")
    print(f"总净盈亏     : {total:+.2f} USDT")
    print(f"平均盈利     : {avg_win:+.2f} / 平均亏损 {avg_loss:+.2f}")
    print(f"盈亏比       : {avg_win/avg_loss if avg_loss else 0:.2f}")
    print(f"期望值/笔    : {total/n if n else 0:+.2f} USDT")
    print(f"最大连续亏损 : {max_dd:+.2f} USDT")
    print("=" * 60)
    verdict = "盈利" if total > 0 else "亏损"
    print(f"结论：该设置在实盘记录区间内 {verdict}（净 {total:+.2f} USDT）")


if __name__ == "__main__":
    main()
