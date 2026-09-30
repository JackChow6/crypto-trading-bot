#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平掉所有持仓 + 撤销所有挂单，清空本地持仓状态。"""
import ccxt, yaml, os, sys, time

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
ex.load_markets()

# 1. 撤销所有挂单（含 Algo 条件单）
print("撤销挂单...")
try:
    opens = ex.fetch_open_orders()
    for o in opens:
        try:
            ex.cancel_order(o["id"], o["symbol"])
            print(f"  撤销挂单 {o['symbol']} {o['id']}")
        except Exception:
            pass
except Exception:
    pass
# 撤销 Algo 条件单（止盈止损）
try:
    algos = ex.fapiPrivateGetOpenAlgoOrders()
    for a in algos:
        try:
            ex.fapiPrivateDeleteAlgoOrder({"algoId": a.get("algoId"), "symbol": a.get("symbol")})
            print(f"  撤销条件单 {a.get('symbol')} {a.get('algoId')}")
        except Exception:
            pass
except Exception:
    pass

# 2. 平掉所有持仓
print("\n平仓...")
positions = ex.fetch_positions()
for p in positions:
    contracts = abs(float(p.get("contracts") or 0))
    if contracts <= 0:
        continue
    sym = p["symbol"]
    side = p.get("side")  # long/short
    close_side = "sell" if side == "long" else "buy"
    amt = ex.amount_to_precision(sym, contracts)
    try:
        ex.create_order(sym, "market", close_side, amt, None, {"reduceOnly": True})
        print(f"  平仓 {sym} {side} {contracts} 张（{close_side}）")
    except Exception as e:
        print(f"  平仓失败 {sym}: {e}")
    time.sleep(0.2)

# 3. 清空本地 live_state.json 的持仓
state_file = os.path.join(BASE, "live_state.json")
try:
    import json
    with open(state_file, "r", encoding="utf-8") as f:
        d = json.load(f)
    d["positions"] = {}
    d["pending_entries"] = {}
    # 拉真实余额校准 equity
    bal = ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    total = float(usdt.get("total") or 0)
    if total > 0:
        d["equity"] = total
        d["peak_equity"] = total
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n已清空本地持仓，equity 校准为 {total}")
except Exception as e:
    print(f"清空本地状态失败: {e}")

print("\n全部平仓完成")
