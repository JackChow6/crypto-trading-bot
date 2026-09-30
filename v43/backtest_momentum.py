#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/backtest_momentum.py —— 动量因子回测（横截面）

验证核心问题：多周期平滑动量排名靠前的币，未来 24h/72h 收益是否显著跑赢全市场？
这是「动量选币」有没有 alpha 的因果检验，与方向信号、止盈止损无关。

方法（参考 Jegadeesh-Titman 动量 + 那篇 crypto 动量实证）：
  1. 拉全市场 USDT 永续，按 24h 成交额取前 N 个最活跃（当前流动性做代理）
  2. 拉每个币最近 D 天 1h K 线
  3. 每个调仓点（每 24h 一次），用过去 1 周数据算「平滑动量强度」
       = (快EMA - 慢EMA) / 波动率   （相当于 MACD / vol，滤掉妖币）
  4. 排名，分 5 组（Q1 最强 … Q5 最弱）
  5. 测每组未来 24h / 72h 平均收益（含手续费）
  6. 输出：Q1 vs Q5 vs 全市场均值，看动量溢价是否存在

用法：python v43/backtest_momentum.py --coins 100 --days 14
"""
from __future__ import annotations
import argparse, time, statistics, sys, os
import concurrent.futures as futures

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT); sys.path.insert(0, BASE)

import yaml, ccxt

FEE = 0.05  # 单边手续费 %


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
    ex.load_markets()
    return ex


def _rest_wait(ex, last=[0.0]):
    now = time.time()
    dt = now - last[0]
    if dt < 0.12:
        time.sleep(0.12 - dt)
    last[0] = time.time()


def fetch_1h(ex, sym, days):
    since = ex.parse8601((int(time.time()) - days * 86400).__str__()) if False else None
    bars = ex.fetch_ohlcv(sym, "1h", limit=days * 24 + 24)
    return [(b[0], b[1], b[2], b[3], b[4]) for b in bars]


def momentum_score(closes):
    """平滑动量 = (EMA_fast - EMA_slow)/EMA_slow / 波动率，窗口约 1 周 vs 3 周。"""
    if len(closes) < 24 * 7:
        return None
    fast = ema(closes, 24)     # ~1 天
    slow = ema(closes, 24 * 7)  # ~1 周
    m = (fast[-1] - slow[-1]) / slow[-1]
    # 波动率：近 3 天收益率标准差
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    vol = statistics.pstdev(rets[-72:]) if len(rets) >= 72 else statistics.pstdev(rets)
    if vol == 0:
        return None
    return m / vol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=100)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-qv", type=float, default=1_000_000)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    ex = build_exchange(cfg)

    # 1) 全市场 ticker → 流动性排序取前 N
    print(f"[1] 拉全市场 ticker，按 24h 成交额取前 {args.coins} 个...")
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
    print(f"    候选 {len(pool)} 个币")

    # 2) 拉 1h K 线（分页，币安单次最多 1000 根）
    print(f"[2] 拉取每个币最近 {args.days} 天 1h K 线...")
    data = {}

    def fetch_paged(sym, days):
        """分页拉 1h K 线，直到覆盖 days 天或达到分页上限。"""
        out = []
        limit = 1000
        since = None
        for _ in range(8):
            _rest_wait(ex)
            if since is None:
                bars = ex.fetch_ohlcv(sym, "1h", limit=limit)
            else:
                bars = ex.fetch_ohlcv(sym, "1h", since=since, limit=limit)
            if not bars:
                break
            out = bars + out
            since = bars[0][0]  # 用最早一根时间戳继续往前拉
            if bars[0][0] <= time.time() * 1000 - days * 86400 * 1000:
                break
            if len(bars) < limit:
                break
        return out

    def _fetch(sym):
        try:
            return sym, fetch_paged(sym, args.days)
        except Exception as e:
            return sym, None

    with futures.ThreadPoolExecutor(max_workers=8) as exe:
        for sym, bars in exe.map(_fetch, pool):
            if bars and len(bars) >= args.days * 24:
                data[sym] = [(b[0], b[1], b[2], b[3], b[4]) for b in bars]
    print(f"    有效 {len(data)} 个币（K线充足）")

    # 3) 回放：每 24h 一个调仓点，用过去 1 周动量排名，测未来 24h/72h 收益
    # 调仓点：从数据起点后第 7 天开始（需要 1 周历史算动量），到最后一天
    horizon_bars = 24  # 24h = 24 根 1h K线
    hold_bars_72 = 72
    # 对齐所有币的时间轴（用每币自己的 bars，按索引回放即可，因为都是 1h 连续）
    n_bars = min(len(b) for b in data.values())
    # 调仓点索引：从第 168 根（7 天）起，每 24 根一次，且留出 72 根测未来
    entry_points = list(range(24 * 7, n_bars - hold_bars_72, 24))

    print(f"[3] 回放：调仓点 {len(entry_points)} 个（每 24h 一次）...")
    fwd24 = {"Q1": [], "Q5": [], "ALL": []}
    fwd72 = {"Q1": [], "Q5": [], "ALL": []}

    for ep in entry_points:
        # 用每币截至 ep 的收盘价算动量
        scores = {}
        for sym, bars in data.items():
            closes = [b[4] for b in bars[:ep + 1]]
            s = momentum_score(closes)
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
            p0 = bars[ep][4]
            p1 = bars[ep + h][4]
            gross = (p1 - p0) / p0 * 100
            return gross - FEE * 2

        for s in q1:
            fwd24["Q1"].append(fwd(s, horizon_bars))
            fwd72["Q1"].append(fwd(s, hold_bars_72))
        for s in q5:
            fwd24["Q5"].append(fwd(s, horizon_bars))
            fwd72["Q5"].append(fwd(s, hold_bars_72))
        for s in scores:
            fwd24["ALL"].append(fwd(s, horizon_bars))
            fwd72["ALL"].append(fwd(s, hold_bars_72))

    # 4) 汇总
    def stat(vals):
        if not vals:
            return "无数据"
        avg = statistics.mean(vals)
        wins = sum(1 for v in vals if v > 0)
        return f"均值 {avg:+.3f}%  胜率 {wins/len(vals)*100:.1f}%  n={len(vals)}"

    print("\n================= 动量因子回测结果 =================")
    print(f"样本：{len(data)} 币 × {len(entry_points)} 个调仓点")
    print(f"\n未来 24h 收益（含手续费 {FEE*2}%）：")
    print(f"  Q1 最强动量组 : {stat(fwd24['Q1'])}")
    print(f"  Q5 最弱动量组 : {stat(fwd24['Q5'])}")
    print(f"  全市场平均    : {stat(fwd24['ALL'])}")
    if fwd24["Q1"] and fwd24["Q5"]:
        spread = statistics.mean(fwd24["Q1"]) - statistics.mean(fwd24["Q5"])
        print(f"  Q1-Q5 动量溢价: {spread:+.3f}%")
    print(f"\n未来 72h 收益（含手续费 {FEE*2}%）：")
    print(f"  Q1 最强动量组 : {stat(fwd72['Q1'])}")
    print(f"  Q5 最弱动量组 : {stat(fwd72['Q5'])}")
    print(f"  全市场平均    : {stat(fwd72['ALL'])}")
    if fwd72["Q1"] and fwd72["Q5"]:
        spread = statistics.mean(fwd72["Q1"]) - statistics.mean(fwd72["Q5"])
        print(f"  Q1-Q5 动量溢价: {spread:+.3f}%")


if __name__ == "__main__":
    main()
