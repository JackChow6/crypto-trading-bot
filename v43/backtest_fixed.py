#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测「固定止盈 1u / 止损 0.5u」策略，用实盘历史成交（decision_log.jsonl）重建价格路径。

用法:
  python backtest_fixed.py          # 按实际方向
  python backtest_fixed.py reverse  # 每笔反向做（long↔short）

对每笔已平仓成交：
  1. 从 outcome 记录拿 symbol/side/entry/exit/pnl/ts(平仓时间)
  2. 反推名义金额 notional = |pnl| / |(exit-entry)/entry|
  3. 把 1u/0.5u 转成价格距离：tp_price = entry*(1 ± 1/notional)，sl_price = entry*(1 ∓ 0.5/notional)
  4. 拉持仓期间 1m K 线，逐根判断先触止盈还是止损
"""
import json
import sys
import time

import ccxt

LOG = "decision_log.jsonl"
PROXY = "http://127.0.0.1:7897"
TP_USD = 1.0
SL_USD = 0.5
# 命令行可覆盖止盈/止损：python backtest_fixed.py reverse 2 1
REVERSE = False
for a in sys.argv[1:]:
    al = a.lower()
    if al == "reverse":
        REVERSE = True
    else:
        try:
            v = float(al)
        except ValueError:
            continue
        if TP_USD == 1.0:
            TP_USD = v
        else:
            SL_USD = v


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


def main():
    outs = load_outcomes()
    mode = "反向做单" if REVERSE else "按实际方向"
    print(f"共 {len(outs)} 笔已平仓成交，回测模式: {mode}\n")

    ex = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "proxies": {"http": PROXY, "https": PROXY},
    })

    rows = []
    actual_pnls = []
    skipped = 0
    for rec in outs:
        sym = rec["symbol"]
        side = rec["side"]
        entry = float(rec["entry"])
        exit_ = float(rec["exit"])
        pnl = float(rec["pnl"])
        close_ts = float(rec["ts"])

        # 反向做单：方向翻转
        if REVERSE:
            side = "short" if side == "long" else "long"
            pnl = -pnl   # 反向后的实际盈亏（供对比）

        notional = infer_notional(entry, exit_, pnl)
        if not notional or notional <= 0:
            skipped += 1
            continue

        if side == "long":
            tp_price = entry * (1 + TP_USD / notional)
            sl_price = entry * (1 - SL_USD / notional)
        else:
            tp_price = entry * (1 - TP_USD / notional)
            sl_price = entry * (1 + SL_USD / notional)

        since_ms = int((close_ts - 24 * 3600) * 1000)
        try:
            ohlcv = ex.fetch_ohlcv(sym, "1m", since=since_ms, limit=1440)
        except Exception:
            skipped += 1
            continue

        # 定位开仓点：收盘价最接近 entry 的那根
        start_i = 0
        best = None
        for i, c in enumerate(ohlcv):
            cclose = float(c[4])
            d = abs(cclose - entry) / entry if entry else 1e9
            if best is None or d < best[0]:
                best = (d, i)
        if best and best[0] < 0.05:
            start_i = best[1]

        result = None
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
                result = "SL"
                break
            if hit_tp and not hit_sl:
                result = "TP"
                break
            if hit_tp and hit_sl:
                result = "AMB"
                break

        if result == "TP":
            fixed_pnl = TP_USD
        elif result == "SL":
            fixed_pnl = -SL_USD
        elif result == "AMB":
            fixed_pnl = None
        else:
            fixed_pnl = pnl  # 未触及，按实际(反向后)盈亏

        rows.append((sym, side, round(pnl, 2), result, fixed_pnl))
        actual_pnls.append(pnl)

    tp_n = sum(1 for r in rows if r[3] == "TP")
    sl_n = sum(1 for r in rows if r[3] == "SL")
    amb_n = sum(1 for r in rows if r[3] == "AMB")
    none_n = sum(1 for r in rows if r[3] is None)

    sum_actual = sum(actual_pnls)

    def fixed_sum(amb_as_sl: bool) -> float:
        s = 0.0
        for sym, side, pnl, res, fp in rows:
            if res == "TP":
                s += TP_USD
            elif res == "SL":
                s += -SL_USD
            elif res == "AMB":
                s += -SL_USD if amb_as_sl else TP_USD
            else:
                s += float(pnl)
        return s

    sum_pess = fixed_sum(True)
    sum_opt = fixed_sum(False)

    print("=" * 60)
    print(f"回测模式: {mode}")
    print(f"样本数: {len(rows)} (跳过无法回测 {skipped} 笔)")
    print(f"TP:{tp_n}  SL:{sl_n}  同根双触AMB:{amb_n}  未触及:{none_n}")
    win_rate = tp_n / len(rows) * 100 if rows else 0.0
    print(f"止盈胜率: {win_rate:.1f}%")
    print(f"反向后的实际总盈亏: {sum_actual:.2f} u")
    print(f"固定策略(悲观,AMB算止损): {sum_pess:.2f} u")
    print(f"固定策略(乐观,AMB算止盈): {sum_opt:.2f} u")


if __name__ == "__main__":
    main()
