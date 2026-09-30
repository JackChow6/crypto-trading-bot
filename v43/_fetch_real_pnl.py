#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用币安 income（资金流）接口还原今天上午的真实盈亏。"""
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

today_start = int(time.time()) - int(time.time()) % 86400
start_time = today_start * 1000

realized = []
commission = 0.0
funding = 0.0
try:
    resp = ex.fapiPrivateGetIncome({"startTime": start_time, "limit": 1000})
except Exception as e:
    print(f"拉取 income 失败: {e}")
    resp = []

for r in resp:
    income_type = r.get("incomeType")
    if income_type == "REALIZED_PNL":
        realized.append({"symbol": r.get("symbol"), "pnl": float(r.get("income")), "time": int(r.get("time"))})
    elif income_type == "COMMISSION":
        commission += float(r.get("income"))
    elif income_type == "FUNDING_FEE":
        funding += float(r.get("income"))

realized.sort(key=lambda x: x["time"])

print(f"币安真实已实现盈亏（今天，{len(realized)} 笔平仓）：\n")
print(f"{'时间':<9}{'币种':<18}{'盈亏(USDT)'}")
print("-" * 40)

total_pnl = 0.0
wins = 0
losses = 0
for r in realized:
    ts = time.strftime('%H:%M:%S', time.localtime(r["time"]/1000))
    pnl = r["pnl"]
    total_pnl += pnl
    if pnl > 0:
        wins += 1
    else:
        losses += 1
    print(f"{ts:<9}{r['symbol']:<18}{pnl:+8.2f}")

print("-" * 40)
print(f"总已实现盈亏: {total_pnl:+.2f} USDT")
print(f"盈利 {wins} 笔 / 亏损 {losses} 笔")
print(f"手续费合计: {commission:.4f} USDT")
print(f"资金费率: {funding:.4f} USDT")
print(f"净盈亏(扣手续费+资金费): {total_pnl + commission + funding:+.2f} USDT")