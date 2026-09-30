#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/gainers.py —— 涨幅榜信号源

抓取交易所 USDT 本位永续合约 24h 涨幅榜前 N 名，作为监控交易对。
方向默认 buy（做多）：涨幅榜币处于强势，跟单方向=多。
"""
from __future__ import annotations


def fetch_top_gainers(exchange, top_n: int = 10, min_quote_volume: float = 0.0, max_gain_pct: float = 0.0) -> list[dict]:
    """返回涨幅榜前 N 名，按 24h 涨幅降序。

    返回: [{"symbol": "XXX/USDT:USDT", "change_pct": 12.3, "last": ..., "quote_volume": ...}, ...]
    max_gain_pct > 0 时，过滤涨幅过高的妖币（避免追到暴涨暴跌的顶端）。
    """
    tickers = exchange.fetch_tickers()
    out = []
    for sym, t in tickers.items():
        try:
            # 只保留 USDT 本位永续（ccxt 统一格式 XXX/USDT:USDT）
            if not str(sym).endswith("/USDT:USDT"):
                continue
            pct = t.get("percentage")
            if pct is None:
                continue
            pct = float(pct)
            if max_gain_pct > 0 and pct > max_gain_pct:
                continue
            qv = float(t.get("quoteVolume") or 0.0)
            if qv < min_quote_volume:
                continue
            out.append({
                "symbol": sym,
                "change_pct": pct,
                "last": t.get("last"),
                "quote_volume": qv,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return out[:top_n]
