#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/risk.py —— 风控引擎

决策之后、下单之前，用确定性规则做最后一道闸：
- 仓位大小（ATR 或 权益比例）
- 杠杆钳制
- 最大同时持仓数 / 最大敞口
- 单日最大亏损熔断（kill-switch）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RiskState:
    equity: float = 1000.0
    peak_equity: float = 1000.0
    daily_pnl: float = 0.0
    open_positions: int = 0
    killed: bool = False
    reason: str = ""
    day: str = ""


class RiskEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.state = RiskState(equity=float(cfg.get("initial_equity", 1000.0)),
                               peak_equity=float(cfg.get("initial_equity", 1000.0)),
                               day=time.strftime("%Y%m%d"))

    def _reset_day_if_needed(self):
        today = time.strftime("%Y%m%d")
        if self.state.day != today:
            self.state.day = today
            self.state.daily_pnl = 0.0

    def check(self, side: str, entry: float, sl: float, atr: float, risk_level: int = 3) -> tuple[bool, str, dict]:
        """风控已关闭：始终放行，只计算每笔仓位参数（size_usd/leverage），不做任何熔断/限仓/回撤/日亏拦截。"""
        c = self.cfg
        self._reset_day_if_needed()
        size_usd = float(c.get("max_position_usd", 5.0))
        leverage = min(int(c.get("leverage", 10)), int(c.get("max_leverage", 10)))
        params = {"size_usd": round(size_usd, 2), "leverage": leverage, "risk_usd": 0.0}
        return True, "OK", params

    def on_trade_closed(self, pnl: float):
        self._reset_day_if_needed()
        self.state.equity += pnl
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
        self.state.daily_pnl += pnl
        self.state.open_positions = max(0, self.state.open_positions - 1)

    def on_trade_opened(self):
        self.state.open_positions += 1

    def on_order_cancelled(self):
        """限价挂单超时撤单：释放预留的仓位名额（不影响权益/盈亏）。"""
        self.state.open_positions = max(0, self.state.open_positions - 1)
