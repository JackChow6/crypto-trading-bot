#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_momentum_scan.py —— 动量窗口扫描

验证哪个动量周期（快/慢均线差 ÷ 波动率）有正的未来收益溢价。
一次拉取数据，扫描多个窗口，输出每个窗口的 Q1(最强)-Q5(最弱) 未来 24h/72h 溢价。

用法：python v43/backtest_momentum_scan.py --coins 220 --days 45
"""
from __future__ import annotations
import argparse, time, statistics, sys, os, json
import concurrent.futures as futures

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import yaml, ccxt

FEE = 0.05  # 单边手续费 %
CACHE = os.path.join(BASE, "momentum_scan_cache.json")

# 窗口：(fast_h, slow_h, 标签)  —— fast/slow 是 1h EMA 周期
WINDOWS = [
    (6, 24, "超短 6h/1天"),
    (24, 72, "短 1天/3天"),
    (24, 168, "短中 1天/1周"),
    (72, 168, "中 3天/1周"),
    (168, 336, "中长 1周/2周"),
    (168, 504, "长 1周/3周"),
]


def ema(values, period):
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def build_exchange(cfg):
    ex = ccxt.binance({
        "options": {"defaultType": "future"},
        "proxies": {"http": cfg["exchange"].get("proxy", ""), "https": cfg["exchange"].get("proxy", "")},
        "enableRateLimit": True,
    })
    return ex


def momentum_score(closes, fast_h, slow_h):
    """平滑动量 = (EMA_fast - EMA_slow)/EMA_slow / 波动率。数据不足返回 None。"""
    if len(closes) < slow_h + 24:
        return None
    fast = ema(closes, fast_h)
    slow = ema(closes, slow_h)
    m = (fast[-1] - slow[-1]) / slow[-1]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    vol = statistics.pstdev(rets[-72:]) if len(rets) >= 72 else statistics.pstdev(rets)
    if not vol:
        return None
    return m / vol


def load_or_fetch(ex, pool, days):
    if os.path.exists(CACHE):
        d = json.load(open(CACHE, encoding="utf-8"))
        if d.get("days") == days and len(d.get("bars", {})) >= 30:
            print(f"[cache] 复用缓存 {len(d['bars'])} 个币")
            return d["bars"]
    print(f"[2] 拉取每个币最近 {days} 天 1h K 线...")
    data = {}
    rate = {"last": 0.0}

    def throttle():
        # 全局限速：每 0.25s 一个请求，避免触发币安 -1003 IP 封禁
        now = time.time()
        dt = now - rate["last"]
        if dt < 0.25:
            time.sleep(0.25 - dt)
        rate["last"] = time.time()

    def fetch_paged(sym):
        out = []
        limit = 1000
        since = None
        for _ in range(8):
            throttle()
            if since is None:
                bars = ex.fetch_ohlcv(sym, "1h", limit=limit)
            else:
                bars = ex.fetch_ohlcv(sym, "1h", since=since, limit=limit)
            if not bars:
                break
            out = bars + out
            since = bars[0][0]
            if bars[0][0] <= time.time() * 1000 - days * 86400 * 1000:
                break
            if len(bars) < limit:
                break
        return out

    def _fetch(sym):
        try:
            return sym, fetch_paged(sym)
        except Exception:
            return sym, None

    # 并发降到 4，配合限速器，确保总请求速率可控
    with futures.ThreadPoolExecutor(max_workers=4) as exe:
        for sym, bars in exe.map(_fetch, pool):
            if bars and len(bars) >= days * 24:
                data[sym] = [(b[0], b[1], b[2], b[3], b[4]) for b in bars]
    json.dump({"days": days, "bars": data}, open(CACHE, "w", encoding="utf-8"))
    print(f"    有效 {len(data)} 个币，已缓存")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=220)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--min-qv", type=float, default=1_000_000)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ex = build_exchange(cfg)

    print(f"[1] 拉全市场 ticker，取前 {args.coins} 个最活跃...")
    tickers = ex.fetch_tickers()
    pool = []
    for sym, t in tickers.items():
        if not str(sym).endswith("/USDT:USDT"):
            continue
        qv = float(t.get("quoteVolume") or 0.0)
        if qv < args.min_qv:
            continue
        pool.append((sym, qv))
    pool.sort(key=lambda x: x[1], reverse=True)
    pool = [s for s, _ in pool[:args.coins]]

    data = load_or_fetch(ex, pool, args.days)

    # 回放：每 24h 一个调仓点，从第 3 周（504h）起，留 72h 测未来
    n_bars = min(len(b) for b in data.values())
    start = 504  # 长窗口需要 3 周历史
    entry_points = list(range(start, n_bars - 72, 24))
    print(f"[3] 回放：调仓点 {len(entry_points)} 个（每 24h 一次，从第 21 天起）\n")

    results = []
    for fast_h, slow_h, label in WINDOWS:
        fwd24_q1, fwd24_q5 = [], []
        fwd72_q1, fwd72_q5 = [], []
        for ep in entry_points:
            scores = {}
            for sym, bars in data.items():
                if len(bars) <= ep:
                    continue
                closes = [b[4] for b in bars[:ep + 1]]
                s = momentum_score(closes, fast_h, slow_h)
                if s is not None:
                    scores[sym] = s
            if len(scores) < 20:
                continue
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            n = len(ranked)
            q1 = [s for s, _ in ranked[: n // 5]]
            q5 = [s for s, _ in ranked[-n // 5:]]

            def fwd(sym, h):
                bars = data[sym]
                p0, p1 = bars[ep][4], bars[ep + h][4]
                return (p1 - p0) / p0 * 100 - FEE * 2

            for s in q1:
                fwd24_q1.append(fwd(s, 24))
                fwd72_q1.append(fwd(s, 72))
            for s in q5:
                fwd24_q5.append(fwd(s, 24))
                fwd72_q5.append(fwd(s, 72))

        if not fwd24_q1 or not fwd24_q5:
            results.append((label, None, None))
            continue
        sp24 = statistics.mean(fwd24_q1) - statistics.mean(fwd24_q5)
        sp72 = statistics.mean(fwd72_q1) - statistics.mean(fwd72_q5)
        wr1 = sum(1 for v in fwd24_q1 if v > 0) / len(fwd24_q1) * 100
        wr5 = sum(1 for v in fwd24_q5 if v > 0) / len(fwd24_q5) * 100
        results.append((label, (sp24, sp72, wr1, wr5, len(fwd24_q1)), None))

    print("=============== 多窗口动量扫描结果 ===============")
    print(f"样本：{len(data)} 币 × {len(entry_points)} 调仓点（含手续费 {FEE*2}%）\n")
    print(f"{'窗口':<18}{'24h溢价':>10}{'72h溢价':>10}{'Q1胜率':>8}{'Q5胜率':>8}{'n':>6}")
    print("-" * 62)
    for label, r, _ in results:
        if r is None:
            print(f"{label:<18}{'样本不足':>30}")
            continue
        sp24, sp72, wr1, wr5, n = r
        print(f"{label:<18}{sp24:>+9.3f}%{sp72:>+9.3f}%{wr1:>7.1f}%{wr5:>7.1f}%{n:>6}")

    # 找最优窗口
    valid = [(lab, r) for lab, r, _ in results if r is not None]
    if valid:
        best = max(valid, key=lambda x: x[1][1])  # 按 72h 溢价
        print(f"\n最优窗口（按 72h 溢价）：{best[0]}，溢价 {best[1][1]:+.3f}%")


if __name__ == "__main__":
    main()
