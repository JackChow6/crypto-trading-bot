#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/signal_filter.py —— 信号过滤（基于当前反向下单阶段的实证特征分析，分方向）

特征分析结论（09-28 22:01 反向下单开启后，225 个「决策→成交」对）：
  1. 做多单：delta_1m 极端负（卖方洪流）时亏损。
       盈利单 delta_1m 均值 -7.5k，亏损单 -51.6k（7 倍）。做多遇卖方洪流 → 拒绝。
  2. 做空单：macd_hist 弱时亏损。
       盈利单 macd_hist 均值 +1.15，亏损单 +0.20（5.6 倍）。做空要求动能足够 → 否则拒绝。
  3. 黑名单：信号反复判错的币（胜率 0% 且笔数 >= 2）。

过滤在「最终实际方向」确定后（reverse 之后）执行。
"""
from __future__ import annotations

# 默认黑名单：当前反向下单阶段胜率 0% 且笔数 >= 2 的币
DEFAULT_BLACKLIST = [
    "Q/USDT:USDT", "AAVE/USDT:USDT", "MUU/USDT:USDT", "SAMSUNG/USDT:USDT",
    "NMR/USDT:USDT", "CRV/USDT:USDT", "ICP/USDT:USDT",
    "NEAR/USDT:USDT", "SNDK/USDT:USDT", "DRAM/USDT:USDT",
]


def _fget(obj, key):
    """兼容 dict 和 dataclass（Trade 对象）。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def dynamic_blacklist(trades, min_trades: int = 2, max_win_rate: float = 0.0) -> list[str]:
    """根据实盘交易记录，动态生成黑名单。

    规则：某币最近成交 >= min_trades 笔，且胜率 <= max_win_rate（默认 0%，即全亏）→ 拉黑。
    只统计最近的交易（按时间倒序取最多 200 笔，避免历史久远数据干扰）。
    """
    recent = sorted(trades, key=lambda t: _fget(t, "closed_at") or _fget(t, "opened_at") or 0)[-200:]
    stats: dict[str, list[float]] = {}
    for t in recent:
        sym = _fget(t, "symbol") or ""
        if not sym:
            continue
        try:
            pnl = float(_fget(t, "pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        stats.setdefault(sym, []).append(pnl)
    out = []
    for sym, pnls in stats.items():
        if len(pnls) < min_trades:
            continue
        wins = sum(1 for p in pnls if p > 0)
        if wins / len(pnls) <= max_win_rate:
            out.append(sym)
    return out


def is_blacklisted(sym: str, trades, cfg: dict | None = None) -> bool:
    """判断某币是否在黑名单（静态 + 动态）。黑名单币改为正向开仓（不反向）。"""
    cfg = cfg or {}
    blacklist = set(cfg.get("blacklist") or DEFAULT_BLACKLIST)
    if cfg.get("dynamic_blacklist", True):
        blacklist.update(dynamic_blacklist(trades or [], cfg.get("bl_min_trades", 2),
                                           cfg.get("bl_max_win_rate", 0.0)))
    return sym in blacklist


def filter_signal(d, snap, cfg: dict | None = None) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝原因)。d 是 ai.Decision，snap 是 market snapshot。

    cfg 可覆盖：
      delta_long_thr: 做多单允许的最小 delta_1m（低于此拒绝，默认 -50000，卖方洪流）
      macd_short_thr: 做空单要求的最小 macd_hist（低于此拒绝，默认 0.3，动能不足）
      blacklist: 静态黑名单币列表（会与动态黑名单合并）
      dynamic_blacklist: 是否启用基于实盘成交的动态黑名单（默认 True）
    """
    cfg = cfg or {}
    action = getattr(d, "action", "NO_TRADE")
    if action not in ("LONG", "SHORT"):
        return True, ""

    sym = snap.get("symbol") or ""
    # 黑名单币直接拒绝开仓
    if is_blacklisted(sym, cfg.get("trades") or [], cfg):
        return False, f"{sym} 在黑名单，禁止开仓"

    flow = snap.get("orderflow") or {}
    macd = snap.get("macd") or {}

    if action == "LONG":
        # 做多单：delta_1m 极端负（卖方洪流）→ 拒绝
        try:
            delta1m = float(flow.get("delta_1m") or 0.0)
        except (TypeError, ValueError):
            return True, ""
        thr = float(cfg.get("delta_long_thr", -50000))
        if delta1m < thr:
            return False, f"delta_1m={delta1m:.0f} 卖方洪流，拒绝做多"
    else:  # SHORT
        # 做空单：macd_hist 动能不足 → 拒绝
        try:
            hist = float(macd.get("hist") or 0.0)
        except (TypeError, ValueError):
            return True, ""
        thr = float(cfg.get("macd_short_thr", 0.3))
        if hist < thr:
            return False, f"macd_hist={hist:.4f} 动能不足，拒绝做空"

    return True, ""
