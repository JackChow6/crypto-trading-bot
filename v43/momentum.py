#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/momentum.py —— 多周期平滑动量选币（替代 24h 涨幅榜）

依据实证结论（加密货币动量效应持续约两周、原始一周涨幅榜无效、平滑多周期信号有效）：
  - 只拉 5m 已收盘 K 线，本地聚合出 15m / 30m / 1h / 4h，省 API 权重
  - 每档算 ret_pct（该周期开盘→收盘涨跌幅），多周期加权得到「平滑动量强度」
  - 做多视角方向化：正向周期加分，反向周期扣一半（避免 5m 强但 1h/4h 反向的假信号）
  - 流动性过滤 + 只取排名两端

参考：charlie-fyz/usdt_perp-kline-screener 的 compute_combined 思路。
"""
from __future__ import annotations

import time

# 多周期权重（短周期反应快、长周期确认趋势；越短权重越高，总和 1.0）
WEIGHTS = {
    "5m": 0.25,
    "15m": 0.25,
    "30m": 0.20,
    "1h": 0.20,
    "4h": 0.10,
}

# 每个周期由多少根 5m K 线聚合（4h = 48 根）
INTERVAL_BARS = {
    "5m": 1,
    "15m": 3,
    "30m": 6,
    "1h": 12,
    "4h": 48,
}


def _ret_pct(candles: list) -> float | None:
    """由一组 5m K 线聚合出一个周期 K 线，返回该周期涨跌幅(%)。

    candles: [[ts, open, high, low, close, vol], ...]，按时间升序。
    返回 (close - open) / open * 100，数据不足返回 None。
    """
    if not candles:
        return None
    o = candles[0][1]
    c = candles[-1][4]
    if o <= 0:
        return None
    return (c - o) / o * 100.0


def fetch_top_momentum(exchange, top_n: int = 30, min_quote_volume: float = 100000.0,
                       pool_size: int = 80, max_workers: int = 8) -> list[dict]:
    """返回多周期平滑动量最强的 Top N（做多视角）。

    返回: [{"symbol": "XXX/USDT:USDT", "change_pct": 综合分(%), "last": ..., "quote_volume": ...}, ...]
    与 gainers.fetch_top_gainers 接口兼容（engine 端只读 symbol / change_pct / last）。
    """
    import concurrent.futures as futures

    # 1) 全市场 ticker：拿 symbol + 24h 成交额（流动性过滤）+ 最新价
    tickers = exchange.fetch_tickers()
    pool = []
    for sym, t in tickers.items():
        try:
            if not str(sym).endswith("/USDT:USDT"):
                continue
            qv = float(t.get("quoteVolume") or 0.0)
            if qv < min_quote_volume:
                continue
            pool.append({"symbol": sym, "quote_volume": qv, "last": t.get("last")})
        except Exception:
            continue
    # 按流动性排序，只对最活跃的 pool_size 个拉 K 线（控制 REST 请求量）
    pool.sort(key=lambda x: x["quote_volume"], reverse=True)
    pool = pool[:pool_size]

    # 2) 并发拉 5m K 线（4h 聚合需要 48 根，多拉 12 根容错 → 60 根）
    since = None
    limit = 60

    def _fetch(p):
        try:
            candles = exchange.fetch_ohlcv(p["symbol"], "5m", since=since, limit=limit)
            return p["symbol"], candles
        except Exception:
            return p["symbol"], None

    klines: dict[str, list] = {}
    with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for sym, candles in ex.map(_fetch, pool):
            if candles:
                klines[sym] = candles

    # 3) 多周期聚合 + 方向化打分
    scored = []
    for p in pool:
        candles = klines.get(p["symbol"])
        if not candles or len(candles) < INTERVAL_BARS["4h"]:
            continue
        rets = {}
        for interval, bars in INTERVAL_BARS.items():
            window = candles[-bars:] if bars <= len(candles) else candles
            rets[interval] = _ret_pct(window)

        # 做多方向化综合分：正向周期全加，反向周期扣一半（避免单周期假强）
        score = 0.0
        for interval, w in WEIGHTS.items():
            r = rets.get(interval)
            if r is None:
                continue
            if r > 0:
                score += r * w
            else:
                score += r * w * 0.5
        scored.append({
            "symbol": p["symbol"],
            "change_pct": round(score, 4),
            "last": p["last"],
            "quote_volume": p["quote_volume"],
            "rets": {k: round(v, 4) if v is not None else None for k, v in rets.items()},
        })

    scored.sort(key=lambda x: x["change_pct"], reverse=True)
    return scored[:top_n]
