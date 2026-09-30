#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/matching.py —— 本地撮合引擎

模拟真实交易所撮合：
- 维护限价订单簿（bids 降序 / asks 升序）
- 市价单逐档吃盘口，产出逐笔成交(Fill)
- 限价单：与当前盘口交叉的部分立即成交，剩余挂单(resting)，
  之后每次刷新盘口时用 match_resting 检查是否被市场触及成交
- 订单生命周期：NEW -> PARTIALLY_FILLED -> FILLED / CANCELLED / REJECTED
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str            # buy / sell
    type: str            # market / limit
    notional: float      # 市价单名义金额(USDT)，限价单为 0
    limit_price: float   # 限价单价格，市价单为 0
    status: str = "NEW"
    filled_notional: float = 0.0
    filled_qty: float = 0.0
    avg_price: float = 0.0
    qty: float = 0.0     # 剩余待成交数量
    created_at: float = field(default_factory=time.time)


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str            # buy / sell
    price: float
    qty: float
    ts: float = field(default_factory=time.time)


class MatchingEngine:
    def __init__(self):
        self.books: dict = {}                # symbol -> {"bids": [[p,q],...], "asks": [[p,q],...]}
        self.orders: list[Order] = []        # 所有订单
        self.fills: list[Fill] = []          # 逐笔成交
        self.resting_orders: list[Order] = []  # 挂单等待成交的限价单
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"ORD-{self._seq:06d}"

    def rebuild_seq(self):
        """根据已有订单号重建序列，避免重启后订单号重号。"""
        max_seq = 0
        for o in self.orders:
            try:
                n = int(str(o.order_id).split("-")[1])
                max_seq = max(max_seq, n)
            except (ValueError, IndexError):
                pass
        self._seq = max_seq

    @staticmethod
    def _norm(levels):
        out = []
        for lv in levels or []:
            try:
                p, q = float(lv[0]), float(lv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if p > 0 and q > 0:
                out.append([p, q])
        return out

    def set_book(self, symbol: str, bids: list, asks: list):
        """用交易所订单簿快照刷新本地订单簿。"""
        self.books[symbol] = {
            "bids": sorted(self._norm(bids), key=lambda x: -x[0]),
            "asks": sorted(self._norm(asks), key=lambda x: x[0]),
        }

    def _avg_price(self, order_id: str) -> float:
        fs = [f for f in self.fills if f.order_id == order_id]
        if not fs:
            return 0.0
        total_cost = sum(f.price * f.qty for f in fs)
        total_qty = sum(f.qty for f in fs)
        return total_cost / total_qty if total_qty > 0 else 0.0

    def available_liquidity(self, symbol: str, side: str) -> float:
        """返回指定方向可成交的名义金额(USDT)。side: buy(吃卖盘)/sell(吃买盘)。"""
        book = self.books.get(symbol)
        if not book:
            return 0.0
        levels = book["asks"] if side == "buy" else book["bids"]
        return sum(p * q for p, q in levels)

    # ------------------------------------------------------------------
    def place_market_order(self, symbol: str, side: str, notional: float) -> Order:
        """下市价单：立即按订单簿逐档撮合，返回 Order。"""
        o = Order(self._next_id(), symbol, side, "market", float(notional), 0.0)
        book = self.books.get(symbol)
        if not book:
            o.status = "REJECTED"
            self.orders.append(o)
            return o
        levels = book["asks"] if side == "buy" else book["bids"]
        remaining = float(notional)
        total_cost = 0.0
        total_qty = 0.0
        i = 0
        while i < len(levels) and remaining > 1e-12:
            price, qty = levels[i]
            level_value = price * qty
            if level_value >= remaining:
                fill_qty = remaining / price
                total_cost += remaining
                total_qty += fill_qty
                self.fills.append(Fill(o.order_id, symbol, side, price, fill_qty))
                levels[i][1] = qty - fill_qty
                if levels[i][1] <= 1e-12:
                    levels.pop(i)
                remaining = 0.0
            else:
                total_cost += level_value
                total_qty += qty
                self.fills.append(Fill(o.order_id, symbol, side, price, qty))
                remaining -= level_value
                levels.pop(i)
        o.filled_notional = float(notional) - remaining
        o.filled_qty = total_qty
        o.avg_price = (total_cost / total_qty) if total_qty > 0 else 0.0
        if remaining <= 1e-12:
            o.status = "FILLED"
        elif total_qty > 0:
            o.status = "PARTIALLY_FILLED"
        else:
            o.status = "REJECTED"
        self.orders.append(o)
        return o

    # ------------------------------------------------------------------
    def place_limit_order(self, symbol: str, side: str, price: float, qty: float) -> Order:
        """下限价单：与当前盘口交叉的部分立即成交，剩余挂单等待市场触及。"""
        o = Order(self._next_id(), symbol, side, "limit", 0.0, float(price))
        book = self.books.setdefault(symbol, {"bids": [], "asks": []})
        remaining_qty = float(qty)
        if side == "buy":
            asks = book["asks"]
            i = 0
            while i < len(asks) and remaining_qty > 1e-12:
                ap, aq = asks[i]
                if ap > price + 1e-12:
                    break
                fill_qty = min(remaining_qty, aq)
                self.fills.append(Fill(o.order_id, symbol, side, ap, fill_qty))
                remaining_qty -= fill_qty
                asks[i][1] = aq - fill_qty
                if asks[i][1] <= 1e-12:
                    asks.pop(i)
                else:
                    i += 1
        else:
            bids = book["bids"]
            i = 0
            while i < len(bids) and remaining_qty > 1e-12:
                bp, bq = bids[i]
                if bp < price - 1e-12:
                    break
                fill_qty = min(remaining_qty, bq)
                self.fills.append(Fill(o.order_id, symbol, side, bp, fill_qty))
                remaining_qty -= fill_qty
                bids[i][1] = bq - fill_qty
                if bids[i][1] <= 1e-12:
                    bids.pop(i)
                else:
                    i += 1
        o.filled_qty = float(qty) - remaining_qty
        o.qty = remaining_qty
        o.avg_price = self._avg_price(o.order_id)
        if remaining_qty <= 1e-12:
            o.status = "FILLED"
        else:
            o.status = "PARTIALLY_FILLED" if o.filled_qty > 0 else "NEW"
            self.resting_orders.append(o)
        self.orders.append(o)
        return o

    # ------------------------------------------------------------------
    def match_resting(self, symbol: str) -> list[Order]:
        """市场刷新后，检查挂单是否被触及成交。返回本次成交的订单列表。"""
        book = self.books.get(symbol)
        if not book:
            return []
        best_ask = book["asks"][0][0] if book["asks"] else None
        best_bid = book["bids"][0][0] if book["bids"] else None
        filled = []
        for o in list(self.resting_orders):
            if o.symbol != symbol:
                continue
            hit = False
            if o.side == "buy" and best_ask is not None and best_ask <= o.limit_price + 1e-12:
                fill_price = best_ask
                hit = True
            elif o.side == "sell" and best_bid is not None and best_bid >= o.limit_price - 1e-12:
                fill_price = best_bid
                hit = True
            if hit:
                self.fills.append(Fill(o.order_id, symbol, o.side, fill_price, o.qty))
                o.filled_qty += o.qty
                o.qty = 0.0
                o.status = "FILLED"
                o.avg_price = self._avg_price(o.order_id)
                self.resting_orders.remove(o)
                filled.append(o)
        return filled

    def cancel_order(self, order_id: str) -> Order | None:
        for o in list(self.resting_orders):
            if o.order_id == order_id:
                o.status = "CANCELLED"
                self.resting_orders.remove(o)
                return o
        return None
