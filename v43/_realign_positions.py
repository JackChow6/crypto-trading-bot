#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用币安真实持仓重建本地 live_state.json 的 positions，消除重复并列仓。

本地 allow_average=false 之前的 bug 导致同一币开了多笔并列仓（本地 28 笔 vs 币安 13 笔）。
本脚本拉币安实际持仓，把本地 positions 重建为「每币一笔」，与币安对齐。
"""
import ccxt, yaml, os, sys, time, json

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
xc = cfg["exchange"]

ex = ccxt.binance({
    "apiKey": xc["api_key"], "secret": xc["api_secret"],
    "enableRateLimit": True, "options": {"defaultType": "swap"},
})
if xc.get("proxy"):
    ex.proxies = {"http": xc["proxy"], "https": xc["proxy"]}

# 拉币安真实持仓
positions = ex.fetch_positions()
actual = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
print(f"币安实际持仓 {len(actual)} 笔：")
for p in actual:
    side = "long" if p.get("side") == "long" else "short"
    entry = float(p.get("entryPrice") or 0)
    contracts = abs(float(p.get("contracts") or 0))
    lev = int(cfg["engine"]["risk"].get("leverage", 20))
    size_usd = round(contracts * entry / lev, 2)
    print(f"  {p['symbol']:<16} {side:<5} entry={entry} size_usd={size_usd}")

# 重建本地 positions：每币一笔
state_file = os.path.join(BASE, "live_state.json")
with open(state_file, "r", encoding="utf-8") as f:
    d = json.load(f)

new_positions = {}
for p in actual:
    sym = p["symbol"]
    side = "long" if p.get("side") == "long" else "short"
    entry = float(p.get("entryPrice") or 0)
    contracts = abs(float(p.get("contracts") or 0))
    lev = int(cfg["engine"]["risk"].get("leverage", 20))
    size_usd = round(contracts * entry / lev, 2)
    # 用固定金额算 sl/tp（1u止盈/2u止损），后续 _apply_fixed_sl_tp 会重设精确值
    tp_usd = float(cfg["engine"].get("fixed_tp_usd", 1.0))
    sl_usd = float(cfg["engine"].get("fixed_sl_usd", 2.0))
    notional = size_usd * lev
    tp_dist = entry * (tp_usd / notional) if notional else 0
    sl_dist = entry * (sl_usd / notional) if notional else 0
    if side == "long":
        sl = round(entry - sl_dist, 8)
        tp = round(entry + tp_dist, 8)
    else:
        sl = round(entry + sl_dist, 8)
        tp = round(entry - tp_dist, 8)
    new_positions[sym] = [{
        "symbol": sym, "side": side, "entry": entry, "size_usd": size_usd,
        "leverage": lev, "sl": sl, "tp": tp, "opened_at": time.time(),
        "highest": entry, "lowest": entry, "tp_active": False, "averages": 0,
        "reversed": False,
    }]

d["positions"] = new_positions
# 校准 equity 用真实余额
try:
    bal = ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    total = float(usdt.get("total") or 0)
    if total > 0:
        d["equity"] = total
        print(f"\nequity 校准为 {total}")
except Exception as e:
    print(f"余额校准失败: {e}")

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)

print(f"\n已重建本地持仓为 {len(new_positions)} 笔（每币一笔），与币安对齐")
