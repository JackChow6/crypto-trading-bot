#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/paper.py —— 模拟盘 + 持仓管理 + 交易日志 + 复盘统计

模拟成交（含滑点）、跟踪持仓、按最新价判定止损/止盈平仓，
记录每笔交易，最后输出胜率/盈亏比/期望/最大回撤等复盘指标。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

import matching


@dataclass
class Position:
    symbol: str
    side: str           # long / short
    entry: float
    size_usd: float
    leverage: int
    sl: float | None
    tp: float | None
    opened_at: float = field(default_factory=time.time)
    highest: float = 0.0
    lowest: float = 0.0
    tp_active: bool = False   # 动态止盈是否已激活（价格触及初始止盈后）
    averages: int = 0         # 已加仓次数（摊低成本）
    reversed: bool = False    # 反向跟单仓位：止盈止损沿用原始信号方向价位（距离互换）

    def unrealized_pnl(self, price: float) -> float:
        if self.side == "long":
            return (price - self.entry) / self.entry * self.size_usd * self.leverage
        return (self.entry - price) / self.entry * self.size_usd * self.leverage


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    exit: float
    pnl: float
    exit_reason: str
    opened_at: float = 0.0
    leverage: int = 1
    bars: int = 0
    closed_at: float = field(default_factory=time.time)


class PaperTrader:
    def __init__(self, cfg: dict, matcher=None):
        self.cfg = cfg or {}
        self.slippage = float(cfg.get("slippage_pct", 0.05)) / 100   # 仅无盘口时兜底
        self.fee = float(cfg.get("fee_pct", 0.05)) / 100             # 吃单手续费(taker)
        self.equity = float(cfg.get("initial_equity", 1000.0))
        self.matcher = matcher or matching.MatchingEngine()
        self.positions: dict[str, list[Position]] = {}   # symbol -> 该币的多笔独立仓位
        self.trades: list[Trade] = []
        self.pending_entries: dict[str, dict] = {}   # symbol -> 挂单元数据（限价入场）
        self.peak_equity = self.equity
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")
        self._lock = threading.RLock()   # 保护 positions/trades/pending_entries 的并发读写
        self._load()

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.equity = d.get("equity", self.equity)
            self.peak_equity = d.get("peak_equity", self.equity)
            self.trades = [Trade(**t) for t in d.get("trades", [])]
            # 兼容旧格式（单仓 dict）与新格式（list），统一迁移为 symbol -> [Position]
            self.positions = {}
            for s, v in (d.get("positions") or {}).items():
                if isinstance(v, list):
                    self.positions[s] = [Position(**p) for p in v]
                elif isinstance(v, dict):
                    self.positions[s] = [Position(**v)]
            # 恢复订单/成交
            self.matcher.orders = [matching.Order(**o) for o in d.get("orders", [])]
            self.matcher.fills = [matching.Fill(**f) for f in d.get("fills", [])]
            self.matcher.rebuild_seq()   # 重建订单号序列，避免重号
            self.matcher.resting_orders = [o for o in self.matcher.orders
                                           if o.type == "limit" and o.status in ("NEW", "PARTIALLY_FILLED")]
            self.pending_entries = d.get("pending_entries", {})
            print(f"[paper] 从磁盘恢复：持仓 {len(self.positions)}，成交 {len(self.trades)}，"
                  f"订单 {len(self.matcher.orders)}，撮合成交 {len(self.matcher.fills)}，挂单 {len(self.pending_entries)}")
        except Exception:
            pass

    def _save(self):
        with self._lock:
            try:
                d = {
                    "equity": self.equity,
                    "peak_equity": self.peak_equity,
                    "trades": [t.__dict__ for t in self.trades],
                    "positions": {s: [p.__dict__ for p in ps] for s, ps in self.positions.items()},
                    "orders": [o.__dict__ for o in self.matcher.orders[-200:]],
                    "fills": [f.__dict__ for f in self.matcher.fills[-500:]],
                    "pending_entries": self.pending_entries,
                }
                tmp = self.state_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.state_file)   # 原子替换，避免进程中途被杀导致文件截断
            except Exception as e:
                print(f"[paper] 保存失败: {e}")

    @staticmethod
    def _adjust_sl_tp(side, entry_ref, fill, sl, tp):
        """按实际成交价重新对齐止盈止损，保持 AI 设定的相对距离。"""
        if sl is None and tp is None:
            return sl, tp
        if side == "long":
            d_sl = (entry_ref - sl) if sl is not None else None
            d_tp = (tp - entry_ref) if tp is not None else None
            sl_adj = (fill - d_sl) if d_sl is not None else None
            tp_adj = (fill + d_tp) if d_tp is not None else None
        else:
            d_sl = (sl - entry_ref) if sl is not None else None
            d_tp = (entry_ref - tp) if tp is not None else None
            sl_adj = (fill + d_sl) if d_sl is not None else None
            tp_adj = (fill - d_tp) if d_tp is not None else None
        return sl_adj, tp_adj

    # --- 开仓（限价单） ---
    def open(self, symbol, side, entry, size_usd, leverage, sl=None, tp=None, bids=None, asks=None, reversed=False):
        # 清理残留：pending_entries 里的订单若已成交/已取消（不在 resting_orders），先移除
        meta = self.pending_entries.get(symbol)
        if meta and not any(o.order_id == meta["order_id"] for o in self.matcher.resting_orders):
            self.pending_entries.pop(symbol, None)
            meta = None
        if meta:
            return None   # 该币有有效挂单，避免重复挂单（已持仓不影响，支持多次开仓）
        if bids or asks:
            self.matcher.set_book(symbol, bids or [], asks or [])
        notional = size_usd * leverage
        side_key = "buy" if side == "long" else "sell"
        # 限价设为对侧盘口价（marketable），确保能立即成交，而不是挂在中间价一直等不到
        if side == "long":
            price = float(asks[0][0]) if asks else entry   # 买单：卖一价
        else:
            price = float(bids[0][0]) if bids else entry   # 卖单：买一价
        qty = notional / price
        order = self.matcher.place_limit_order(symbol, side_key, price, qty)
        if order.status == "FILLED":
            # 当前价已触及限价 → 立即成交开仓
            fill = order.avg_price * (1 + self.fee) if side == "long" else order.avg_price * (1 - self.fee)
            sl_adj, tp_adj = self._adjust_sl_tp(side, entry, fill, sl, tp)
            print(f"[paper] {symbol} 限价单立即成交：{order.order_id} 均价={order.avg_price:.8g}")
            pos = Position(symbol=symbol, side=side, entry=fill, size_usd=size_usd,
                           leverage=leverage, sl=sl_adj, tp=tp_adj, highest=fill, lowest=fill,
                           reversed=reversed)
            self.positions.setdefault(symbol, []).append(pos)
            self._save()
            return pos
        # 未立即成交 → 挂单等待
        self.pending_entries[symbol] = {
            "order_id": order.order_id, "side": side, "entry": entry,
            "size_usd": size_usd, "leverage": leverage, "sl": sl, "tp": tp,
            "created_at": time.time(), "reversed": reversed,
        }
        print(f"[paper] {symbol} 限价单已挂：{order.order_id} {side} @ {price}，等待价格触及")
        self._save()
        return "PENDING"   # 挂单等待（区别于 None=已有持仓/挂单）

    # --- 检查挂单是否成交/超时 ---
    def settle_pending(self, symbol, bids=None, asks=None):
        if symbol not in self.pending_entries:
            return None
        meta = self.pending_entries[symbol]
        if bids or asks:
            self.matcher.set_book(symbol, bids or [], asks or [])
        filled = self.matcher.match_resting(symbol)
        for fo in filled:
            if fo.order_id == meta["order_id"]:
                side = meta["side"]
                fill = fo.avg_price * (1 + self.fee) if side == "long" else fo.avg_price * (1 - self.fee)
                sl_adj, tp_adj = self._adjust_sl_tp(side, meta["entry"], fill, meta["sl"], meta["tp"])
                print(f"[paper] {symbol} 限价单成交开仓：{fo.order_id} 均价={fo.avg_price:.8g}")
                pos = Position(symbol=symbol, side=side, entry=fill, size_usd=meta["size_usd"],
                               leverage=meta["leverage"], sl=sl_adj, tp=tp_adj,
                               highest=fill, lowest=fill, reversed=bool(meta.get("reversed", False)))
                self.positions.setdefault(symbol, []).append(pos)
                del self.pending_entries[symbol]
                self._save()
                return pos
        # 超时撤单
        timeout = float(self.cfg.get("limit_timeout_sec", 600))
        if time.time() - meta["created_at"] > timeout:
            self.matcher.cancel_order(meta["order_id"])
            print(f"[paper] {symbol} 限价单超时撤单：{meta['order_id']}")
            del self.pending_entries[symbol]
            self._save()
            return "CANCELLED"
        return None

    def cancel_pending(self, symbol):
        meta = self.pending_entries.pop(symbol, None)
        if meta:
            self.matcher.cancel_order(meta["order_id"])
            print(f"[paper] {symbol} 撤掉挂单：{meta['order_id']}")
            self._save()

    # --- 加仓摊低成本 ---
    def average(self, symbol, side, add_price, add_size_usd, leverage,
                sl_dist=None, tp_dist=None) -> Position | None:
        """同方向加仓：对最近一笔同方向仓位加权平均入场价（摊低成本），并按距离重锚 SL/TP。"""
        plist = self.positions.get(symbol) or []
        pos = None
        for p in reversed(plist):
            if p.side == side:
                pos = p
                break
        if not pos:
            return None
        total = pos.size_usd + add_size_usd
        pos.entry = (pos.entry * pos.size_usd + add_price * add_size_usd) / total
        pos.size_usd = total
        pos.averages += 1
        if sl_dist is not None:
            pos.sl = round(pos.entry - sl_dist, 8) if side == "long" else round(pos.entry + sl_dist, 8)
        if tp_dist is not None:
            pos.tp = round(pos.entry + tp_dist, 8) if side == "long" else round(pos.entry - tp_dist, 8)
        # 加仓后重置高低点基准：移动止损从「加仓后的成交价」重新追踪，
        # 避免用加仓前的历史高点 + 新均价，把止损瞬间抬到离现价极近的位置误扫。
        pos.highest = add_price
        pos.lowest = add_price
        self._save()
        return pos

    # --- 每根K线结算：判定止盈/止损/移动止损（遍历该币所有仓位） ---
    def mark(self, symbol, price, high=None, low=None, bids=None, asks=None,
             trail_dist=None, break_even_dist=None, fixed_mode=False,
             trail_tight_dist=None, tighten_dist=None) -> list[Trade]:
        with self._lock:
            return self._mark_locked(symbol, price, high, low, bids, asks,
                                     trail_dist, break_even_dist, fixed_mode,
                                     trail_tight_dist, tighten_dist)

    def _mark_locked(self, symbol, price, high=None, low=None, bids=None, asks=None,
                     trail_dist=None, break_even_dist=None, fixed_mode=False,
                     trail_tight_dist=None, tighten_dist=None) -> list[Trade]:
        plist = self.positions.get(symbol)
        if not plist:
            return []
        # 用实时价判断止损/移动止损，不用 K 线历史影线：
        # K 线周期(15m)的最低点可能发生在开仓之前，用它判止损会刚开仓就被历史低点误杀。
        closed: list[Trade] = []
        for pos in list(plist):
            notional = pos.size_usd * pos.leverage
            # 固定止盈止损模式：触及 TP 立即平仓、触及 SL 立即平仓，不做移动止损/动态止盈
            if fixed_mode:
                if pos.side == "long":
                    if pos.tp and price >= pos.tp:
                        closed.append(self._close(symbol, pos, self._exit_fill(symbol, "sell", notional, bids, asks, price, pos), "TP"))
                    elif pos.sl and price <= pos.sl:
                        closed.append(self._close(symbol, pos, self._exit_fill(symbol, "sell", notional, bids, asks, price, pos), "STOP_LOSS"))
                else:
                    if pos.tp and price <= pos.tp:
                        closed.append(self._close(symbol, pos, self._exit_fill(symbol, "buy", notional, bids, asks, price, pos), "TP"))
                    elif pos.sl and price >= pos.sl:
                        closed.append(self._close(symbol, pos, self._exit_fill(symbol, "buy", notional, bids, asks, price, pos), "STOP_LOSS"))
                continue
            if pos.side == "long":
                pos.highest = max(pos.highest, price)
                # 动态止盈锁利：触及止盈目标 → 止损抬到止盈位锁住目标利润(2u)，清空 TP，交给移动止损继续跑
                if pos.tp and price >= pos.tp:
                    pos.sl = max(pos.sl or 0, pos.tp)
                    pos.tp = None
                # 分级移动止损（trailing_stop_positive 语义）：浮盈越大，回撤容忍越小
                if trail_dist:
                    eff = trail_dist
                    # 浮盈超过 tighten_dist 后，改用更紧的 trail_tight_dist，把大利润锁死
                    if trail_tight_dist and tighten_dist and pos.highest >= pos.entry + tighten_dist:
                        eff = trail_tight_dist
                    pos.sl = max(pos.sl or 0, pos.highest - eff)
                # 保本（可选，锁利前的第一道防线）
                if break_even_dist and pos.highest >= pos.entry + break_even_dist:
                    pos.sl = max(pos.sl or 0, pos.entry)
                # 平仓判定：止损优先（实时价）
                if pos.sl and price <= pos.sl:
                    closed.append(self._close(symbol, pos, self._exit_fill(symbol, "sell", notional, bids, asks, price, pos), "STOP_LOSS"))
            else:
                pos.lowest = min(pos.lowest, price)
                # 做空锁利：触及止盈目标 → 止损下移到止盈位锁利
                if pos.tp and price <= pos.tp:
                    pos.sl = min(pos.sl or float("inf"), pos.tp)
                    pos.tp = None
                if trail_dist:
                    eff = trail_dist
                    if trail_tight_dist and tighten_dist and pos.lowest <= pos.entry - tighten_dist:
                        eff = trail_tight_dist
                    pos.sl = min(pos.sl or float("inf"), pos.lowest + eff)
                if break_even_dist and pos.lowest <= pos.entry - break_even_dist:
                    pos.sl = min(pos.sl or float("inf"), pos.entry)
                if pos.sl and price >= pos.sl:
                    closed.append(self._close(symbol, pos, self._exit_fill(symbol, "buy", notional, bids, asks, price, pos), "STOP_LOSS"))
        return closed

    def _exit_fill(self, symbol, side, notional, bids, asks, price, pos=None) -> float:
        """平仓时下市价单撮合，返回含手续费的成交价。"""
        if bids or asks:
            self.matcher.set_book(symbol, bids or [], asks or [])
        order = self.matcher.place_market_order(symbol, side, notional)
        vwap = order.avg_price if order.avg_price > 0 else price * (1 - self.slippage)
        return vwap * (1 - self.fee) if side == "sell" else vwap * (1 + self.fee)

    def _close(self, symbol, pos: Position, exit_price, reason) -> Trade:
        plist = self.positions.get(symbol)
        if plist and pos in plist:
            plist.remove(pos)
        if not plist:
            self.positions.pop(symbol, None)
        pnl = pos.unrealized_pnl(exit_price)
        self.equity += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        t = Trade(symbol, pos.side, pos.entry, exit_price, round(pnl, 2), reason,
                  opened_at=pos.opened_at, leverage=pos.leverage)
        self.trades.append(t)
        self._save()
        return t

    def close_position(self, symbol, price, bids=None, asks=None, reason="MANUAL_CLOSE") -> list[Trade]:
        """按当前市价平掉该币所有仓位（dashboard 按钮 / K 线反转及时止盈等场景），返回平掉的成交列表。"""
        with self._lock:
            plist = self.positions.get(symbol)
            if not plist:
                return []
            closed = []
            for pos in list(plist):
                notional = pos.size_usd * pos.leverage
                side = "sell" if pos.side == "long" else "buy"   # 平多=卖，平空=买
                exit_fill = self._exit_fill(symbol, side, notional, bids, asks, price, pos)
                closed.append(self._close(symbol, pos, exit_fill, reason))
            return closed

    # --- 复盘统计 ---
    def stats(self) -> dict:
        tr = self.trades
        wins = [t for t in tr if t.pnl > 0]
        losses = [t for t in tr if t.pnl <= 0]
        gross_w = sum(t.pnl for t in wins)
        gross_l = abs(sum(t.pnl for t in losses))
        n = len(tr)
        return {
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 2) if n else 0.0,
            "avg_win": round(gross_w / len(wins), 2) if wins else 0.0,
            "avg_loss": round(gross_l / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else (999.0 if gross_w else 0.0),
            "net_pnl": round(sum(t.pnl for t in tr), 2),
            "max_drawdown_pct": round((self.equity / self.peak_equity - 1) * 100, 2),
        }
