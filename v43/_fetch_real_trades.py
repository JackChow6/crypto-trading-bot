#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取币安账户真实成交记录（今天上午）。"""
import ccxt, yaml, os, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
xc = cfg["exchange"]

ex = ccxt.binance({
    "apiKey": xc["api_key"],
    "secret": xc["api_secret"],
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
if xc.get("proxy"):
    ex.proxies = {"http": xc["proxy"], "https": xc["proxy"]}
ex.load_markets()

# 今天 00:00 UTC 起
today_start = int(time.time()) - int(time.time()) % 86400
print(f"拉取币安成交记录（自 {time.strftime('%Y-%m-%d %H:%M', time.gmtime(today_start))} UTC）...\n")

# 逐币拉取（币安 fetch_my_trades 需要按 symbol）
# 先拉所有持仓过的币种
syms = ["WOO/USDT:USDT", "CETUS/USDT:USDT", "GRASS/USDT:USDT", "PUMP/USDT:USDT",
        "JASMY/USDT:USDT", "NOM/USDT:USDT", "TAIKO/USDT:USDT", "INX/USDT:USDT",
        "W/USDT:USDT", "BTW/USDT:USDT", "TRUST/USDT:USDT", "METIS/USDT:USDT",
        "CHR/USDT:USDT", "EVAA/USDT:USDT", "FOLKS/USDT:USDT", "PHAROS/USDT:USDT",
        "AGT/USDT:USDT", "ALCH/USDT:USDT", "SUPER/USDT:USDT", "WLD/USDT:USDT"]

all_trades = []
for sym in syms:
    try:
        if sym not in ex.markets:
            continue
        trades = ex.fetch_my_trades(sym, since=today_start * 1000, limit=1000)
        all_trades.extend(trades)
        time.sleep(0.2)
    except Exception as e:
        print(f"  {sym} 拉取失败: {e}")

# 按时间排序
all_trades.sort(key=lambda t: t["timestamp"])
print(f"共 {len(all_trades)} 笔成交\n")
print(f"{'时间':<9}{'方向':<6}{'币种':<18}{'价格':<12}{'数量':<12}{'金额':<10}{'手续费'}")
print("-" * 80)

total_cost = 0.0
for t in all_trades:
    ts = time.strftime('%H:%M:%S', time.localtime(t["timestamp"]/1000))
    side = t["side"]
    sym_short = t["symbol"].split(":")[0]
    price = t["price"]
    amount = t["amount"]
    cost = t.get("cost") or (price * amount)
    fee = (t.get("fee") or {}).get("cost", 0) if t.get("fee") else 0
    total_cost += fee
    print(f"{ts:<9}{side:<6}{sym_short:<18}{price:<12.8g}{amount:<12.8g}{cost:<10.2f}{fee:.4f}")

print("-" * 80)
print(f"总手续费: {total_cost:.4f} USDT")

# 拉取当前真实余额
try:
    bal = ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    print(f"\n当前账户 USDT 余额: {usdt.get('total')}")
except Exception as e:
    print(f"\n余额拉取失败: {e}")