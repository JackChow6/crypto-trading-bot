#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/live.py —— 币安实盘交易执行层（LiveTrader）

继承 PaperTrader 的位置管理 / 移动止损止盈 / 复盘统计逻辑，只把「成交」替换成真实 ccxt 下单：
  - 开仓：真实市价单
  - 平仓：真实 reduce-only 市价单
  - 加仓：真实市价单

状态保存到 live_state.json（与模拟盘 paper_state.json 分离）。
依赖 config 里填好 binance 的 api_key / api_secret，并在 engine 下开 live: true。
"""
from __future__ import annotations

import json
import os
import threading
import time

import matching
import paper as papermod


class LiveTrader(papermod.PaperTrader):
    def __init__(self, cfg: dict, exchange):
        self.cfg = cfg or {}
        self.exchange = exchange
        self.fee = float(cfg.get("fee_pct", 0.05)) / 100
        self.equity = float(cfg.get("initial_equity", 1000.0))
        self.positions: dict = {}
        self.trades: list = []
        self.pending_entries: dict = {}
        self.peak_equity = self.equity
        self.matcher = matching.MatchingEngine()   # 空撮合引擎（仅兼容 dashboard 读取 fills/orders）
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_state.json")
        self._lock = threading.RLock()   # 与 PaperTrader 一致：保护 positions/trades 并发读写
        self._load()

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.equity = d.get("equity", self.equity)
            self.peak_equity = d.get("peak_equity", self.equity)
            self.trades = [papermod.Trade(**t) for t in d.get("trades", [])]
            # 兼容旧格式（单仓 dict）与新格式（list），统一迁移为 symbol -> [Position]
            self.positions = {}
            for s, v in (d.get("positions") or {}).items():
                if isinstance(v, list):
                    self.positions[s] = [papermod.Position(**p) for p in v]
                elif isinstance(v, dict):
                    self.positions[s] = [papermod.Position(**v)]
            self.pending_entries = d.get("pending_entries", {})
            print(f"[live] 恢复实盘状态：持仓 {len(self.positions)}，成交 {len(self.trades)}")
        except Exception:
            pass

    def _save(self):
        try:
            d = {
                "equity": self.equity,
                "peak_equity": self.peak_equity,
                "trades": [t.__dict__ for t in self.trades],
                "positions": {s: [p.__dict__ for p in ps] for s, ps in self.positions.items()},
                "orders": [],
                "fills": [],
                "pending_entries": self.pending_entries,
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[live] 保存状态失败: {e}")

    def _prepare(self, symbol, leverage):
        """设置逐仓/全仓与杠杆（幂等，失败不阻断下单）。"""
        try:
            self.exchange.set_margin_mode(self.cfg.get("margin_mode", "cross"), symbol)
        except Exception:
            pass
        try:
            self.exchange.set_leverage(int(leverage), symbol)
        except Exception:
            pass

    def _position_qty(self, symbol):
        """查询该 symbol 当前持仓数量(contracts)，用于挂 reduceOnly 条件单。"""
        try:
            pos = self.exchange.fetch_positions([symbol])
            for p in pos:
                c = abs(float(p.get("contracts") or 0))
                if c > 0:
                    return float(self.exchange.amount_to_precision(symbol, c))
        except Exception:
            pass
        return None

    def _place_tp_sl(self, symbol, side, tp_price, sl_price, qty=None, place_tp=True):
        """开仓后在币安服务器端挂止盈/止损条件单。

        币安 USDT-M 合约必须用 Algo Order API（create_stop_loss_order / create_take_profit_order）。
        用 reduceOnly + 精确数量（无 closePosition 唯一单限制，可先挂新再撤旧）。
        流程：先挂新单（成功）→ 再取消该币多余的旧条件单（仅保留刚挂的），失败不导致裸奔。
        qty 已知时直接传入（开仓场景），避免额外 fetch_positions 反查。

        place_tp=False 时只挂止损、不挂止盈：用于「动态止盈」模式。
        动态止盈 = 触及 2u 目标后不立即平仓，而是锁住利润继续奔跑（本地移动止损跟进），
        因此服务器端不能挂固定 TP 单（否则触及即被币安直接平仓，动态锁利无机会触发）。
        """
        close_side = "sell" if side == "long" else "buy"   # 平多=卖，平空=买
        if qty is None:
            qty = self._position_qty(symbol)
        if not qty:
            print(f"[live] 挂止盈止损失败 {symbol}: 查询不到持仓数量")
            return
        placed_ids = set()
        # 先挂新单（成功才记录 id），失败不影响已有旧单
        try:
            if sl_price:
                sl_price = float(self.exchange.price_to_precision(symbol, sl_price))
                self._rest_wait()
                r = self.exchange.create_stop_loss_order(symbol, "market", close_side, qty,
                                                         None, sl_price, {"reduceOnly": True})
                placed_ids.add(str(r.get("id")))
                print(f"[live] 已挂服务器端止损 {symbol} @ {sl_price} qty={qty} id={r.get('id')}")
        except Exception as e:
            print(f"[live] 挂止损单失败 {symbol}: {e}")
        try:
            if tp_price and place_tp:
                tp_price = float(self.exchange.price_to_precision(symbol, tp_price))
                self._rest_wait()
                r = self.exchange.create_take_profit_order(symbol, "market", close_side, qty,
                                                           None, tp_price, {"reduceOnly": True})
                placed_ids.add(str(r.get("id")))
                print(f"[live] 已挂服务器端止盈 {symbol} @ {tp_price} qty={qty} id={r.get('id')}")
        except Exception as e:
            print(f"[live] 挂止盈单失败 {symbol}: {e}")
        # 挂完新单后，取消该币除刚挂之外的多余旧条件单（幂等清理，不误删刚挂的）
        self._cancel_tp_sl_except(symbol, placed_ids)

    def _sync_server_sl(self, symbol, side, sl_price, qty=None):
        """动态止盈：本地锁利后，把「止损上移到锁利位」同步到服务器端。

        只更新 SL 单（先挂新止损再撤旧止损），不动 TP（动态模式下服务器端本就无 TP 单）。
        这样引擎断线时服务器端仍按最新锁利位止损，不会裸奔。
        """
        if not sl_price:
            return
        close_side = "sell" if side == "long" else "buy"
        if qty is None:
            qty = self._position_qty(symbol)
        if not qty:
            print(f"[live] 同步止损失败 {symbol}: 查询不到持仓数量")
            return
        try:
            sl_price = float(self.exchange.price_to_precision(symbol, sl_price))
            self._rest_wait()
            r = self.exchange.create_stop_loss_order(symbol, "market", close_side, qty,
                                                     None, sl_price, {"reduceOnly": True})
            new_id = str(r.get("id"))
            print(f"[live] 动态止盈锁利：止损上移到 {symbol} @ {sl_price} qty={qty}")
            # 撤掉该币其它旧止损单（保留刚挂的），避免多个 SL 叠加重复触发
            usym = symbol.replace("/USDT:USDT", "USDT").replace(":USDT", "")
            try:
                self._rest_wait()
                algos = self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": usym})
                for a in algos:
                    algo_id = str(a.get("algoId"))
                    # 只清 STOP_LOSS 类型的旧单，保留（若有）TAKE_PROFIT
                    if algo_id and algo_id != new_id and "STOP" in str(a.get("algoType") or "").upper():
                        try:
                            self._rest_wait()
                            self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": algo_id, "symbol": usym})
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception as e:
            print(f"[live] 同步止损失败 {symbol}: {e}")

    def _cancel_tp_sl_except(self, symbol, keep_ids: set):
        """取消该币所有 Algo/条件单，但保留 keep_ids 里的单（用于先挂新再清旧）。"""
        usym = symbol.replace("/USDT:USDT", "USDT").replace(":USDT", "")
        try:
            self._rest_wait()
            algos = self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": usym})
            for a in algos:
                algo_id = str(a.get("algoId"))
                if algo_id and algo_id not in keep_ids:
                    try:
                        self._rest_wait()
                        self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": algo_id, "symbol": usym})
                    except Exception:
                        pass
        except Exception:
            pass

    def _qty(self, symbol, notional, price):
        qty = notional / price if price else 0.0
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            return qty

    def _apply_average(self, symbol, side, add_price, add_size_usd, sl_dist=None, tp_dist=None):
        """把一笔成交合并进最近一笔同方向持仓（加权平均入场价 + 重锚 SL/TP），不下单。"""
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

    def _recalc_sl_tp(self, pos):
        """按「止盈固定金额 + 止损动态金额」重新计算该仓位的止盈/止损绝对价。

        止盈：fixed_tp_usd 是整笔持仓的盈亏金额，距离 = 金额 / notional × entry。
        止损：dynamic_sl=true 时按 ATR 波动率自适应（clamp 到 [sl_min_usd, sl_max_usd]），
        否则用固定 fixed_sl_usd。合并/加仓后名义变大，同样金额对应的价格距离变小，需重算。
        """
        fixed_tp = float(getattr(self, "fixed_tp_usd", 0.0) or 0.0)
        fixed_sl = float(getattr(self, "fixed_sl_usd", 0.0) or 0.0)
        if fixed_tp <= 0 or fixed_sl <= 0:
            return
        notional = pos.size_usd * pos.leverage
        if notional <= 0:
            return
        tp_dist = pos.entry * (fixed_tp / notional)
        # 动态止损金额（多时间框架：优先用止损周期 ATR 回调，回退主周期 atr_cache）
        sl_usd = fixed_sl
        if getattr(self, "dynamic_sl", False):
            atr = 0.0
            sl_atr_fn = getattr(self, "sl_atr_fn", None)
            if callable(sl_atr_fn):
                try:
                    atr = sl_atr_fn(pos.symbol) or 0.0
                except Exception:
                    atr = 0.0
            if not atr:
                atr = (getattr(self, "atr_cache", {}) or {}).get(pos.symbol, 0.0)
            if atr and notional and pos.entry:
                sl_usd = atr * float(getattr(self, "sl_atr_mult", 2.0)) * notional / pos.entry
            sl_min = float(getattr(self, "sl_min_usd", 0.0) or 0.0)
            sl_max = float(getattr(self, "sl_max_usd", 0.0) or 0.0)
            if sl_min > 0:
                sl_usd = max(sl_usd, sl_min)
            if sl_max > 0:
                sl_usd = min(sl_usd, sl_max)
        sl_dist = pos.entry * (sl_usd / notional)
        if pos.side == "long":
            pos.sl = round(pos.entry - sl_dist, 8)
            pos.tp = round(pos.entry + tp_dist, 8)
        else:
            pos.sl = round(pos.entry + sl_dist, 8)
            pos.tp = round(pos.entry - tp_dist, 8)

    # --- 真实开仓（市价单 或 限价吃回踩 或 分批） ---
    def open(self, symbol, side, entry, size_usd, leverage, sl=None, tp=None, bids=None, asks=None, reversed=False):
        if symbol in self.pending_entries:
            return None   # 该币有有效挂单，避免重复挂单（已持仓不影响，支持多次开仓）
        self._prepare(symbol, leverage)
        side_key = "buy" if side == "long" else "sell"
        qty = self._qty(symbol, size_usd * leverage, entry)

        # 入场方式
        order_type = str(self.cfg.get("entry_order_type", "market")).lower()
        offset = float(self.cfg.get("entry_limit_offset_pct", 0)) / 100
        # SL/TP 距离（限价半仓成交后重锚用）
        if side == "long":
            sl_dist = (entry - sl) if sl is not None else None
            tp_dist = (tp - entry) if tp is not None else None
        else:
            sl_dist = (sl - entry) if sl is not None else None
            tp_dist = (entry - tp) if tp is not None else None

        # 分批入场：一半市价 + 一半限价吃更优价格，摊平入场
        if order_type == "split":
            ratio = max(0.1, min(0.9, float(self.cfg.get("entry_split_ratio", 0.5))))
            market_size = round(size_usd * ratio, 2)
            limit_size = round(size_usd - market_size, 2)
            mqty = self._qty(symbol, market_size * leverage, entry)
            try:
                mo = self.exchange.create_order(symbol, "market", side_key, mqty)
            except Exception as e:
                print(f"[live] 分批市价半仓失败 {symbol} {side}: {e}")
                return None
            mfill = float(mo.get("average") or mo.get("price") or entry)
            msl, mtp = self._adjust_sl_tp(side, entry, mfill, sl, tp)
            pos = papermod.Position(symbol=symbol, side=side, entry=mfill, size_usd=market_size,
                                    leverage=leverage, sl=msl, tp=mtp, highest=mfill, lowest=mfill,
                                    reversed=reversed)
            self.positions.setdefault(symbol, []).append(pos)
            # 市价半仓成交后立即挂服务器端 TP/SL（避免限价半仓未成交期间裸奔）
            self._place_tp_sl(symbol, side, mtp, msl, qty=mqty, place_tp=not getattr(self, "dynamic_tp", False))
            if limit_size > 0:
                # Maker 挂单赚返佣：限价半仓直接挂盘口最优买一价（做多）/卖一价（做空）。
                # 挂盘口价几乎必成交（贴盘口），同时是 maker 单吃手续费返佣，
                # 不再用 offset 折扣价（那个离盘口远，成交率低）。
                if side == "long":
                    limit_price = (bids[0][0] if bids else None) or entry
                else:
                    limit_price = (asks[0][0] if asks else None) or entry
                try:
                    limit_price = float(self.exchange.price_to_precision(symbol, limit_price))
                except Exception:
                    pass
                lqty = self._qty(symbol, limit_size * leverage, entry)
                try:
                    lo = self.exchange.create_order(symbol, "limit", side_key, lqty, limit_price)
                except Exception as e:
                    print(f"[live] 分批限价半仓挂单失败 {symbol}: {e}")
                else:
                    if lo.get("status") == "closed":
                        lfill = float(lo.get("average") or lo.get("price") or limit_price)
                        self._apply_average(symbol, side, lfill, limit_size, sl_dist, tp_dist)
                        print(f"[live] 限价半仓立即成交 {symbol} @ {lfill:.8g}")
                    else:
                        self.pending_entries[symbol] = {
                            "order_id": lo.get("id"), "side": side, "entry": entry,
                            "size_usd": limit_size, "leverage": leverage, "sl": sl, "tp": tp,
                            "created_at": time.time(), "add": True,
                            "sl_dist": sl_dist, "tp_dist": tp_dist,
                            "limit_price": limit_price, "reversed": reversed,
                        }
                        print(f"[live] 限价半仓挂单 {symbol} @ {limit_price}，等待回踩")
            self._save()
            print(f"[live] 分批入场：市价半仓 {symbol} {side} @ {mfill:.8g} size={market_size}")
            return pos

        if order_type == "limit" and offset > 0:
            limit_price = entry * (1 - offset) if side == "long" else entry * (1 + offset)
            try:
                limit_price = float(self.exchange.price_to_precision(symbol, limit_price))
            except Exception:
                pass
            try:
                order = self.exchange.create_order(symbol, "limit", side_key, qty, limit_price)
            except Exception as e:
                print(f"[live] 限价挂单失败 {symbol} {side}: {e}")
                return None
            if order.get("status") == "closed":
                fill = float(order.get("average") or order.get("price") or limit_price)
                sl_adj, tp_adj = self._adjust_sl_tp(side, entry, fill, sl, tp)
                pos = papermod.Position(symbol=symbol, side=side, entry=fill, size_usd=size_usd,
                                        leverage=leverage, sl=sl_adj, tp=tp_adj, highest=fill, lowest=fill,
                                        reversed=reversed)
                self.positions.setdefault(symbol, []).append(pos)
                self._save()
                print(f"[live] 限价单立即成交 {symbol} {side} @ {fill:.8g}")
                return pos
            self.pending_entries[symbol] = {
                "order_id": order.get("id"), "side": side, "entry": entry,
                "size_usd": size_usd, "leverage": leverage, "sl": sl, "tp": tp,
                "created_at": time.time(), "reversed": reversed,
            }
            self._save()
            print(f"[live] 限价挂单 {symbol} {side} @ {limit_price}，等待回踩成交")
            return "PENDING"

        # 市价入场（币安 USDT-M 合约不支持开仓单直接附带 stopLossPrice/takeProfitPrice，
        # 会报 -2021。改为：先市价开仓，成交后单独挂 reduce-only 的止盈/止损条件单）
        try:
            order = self.exchange.create_order(symbol, "market", side_key, qty)
        except Exception as e:
            print(f"[live] 开仓下单失败 {symbol} {side}: {e}")
            return None
        fill = float(order.get("average") or order.get("price") or entry)
        sl_adj, tp_adj = self._adjust_sl_tp(side, entry, fill, sl, tp)
        pos = papermod.Position(symbol=symbol, side=side, entry=fill, size_usd=size_usd,
                                leverage=leverage, sl=sl_adj, tp=tp_adj, highest=fill, lowest=fill,
                                reversed=reversed)
        self.positions.setdefault(symbol, []).append(pos)
        self._save()
        print(f"[live] 开仓成功 {symbol} {side} qty={qty} 均价={fill:.8g}")
        # 成交后挂服务器端止盈/止损条件单（reduce-only，平仓后由对账/平仓逻辑清理残留）
        # 动态止盈模式：服务器端只挂止损，不挂固定止盈（触及目标由本地锁利继续奔跑）
        self._place_tp_sl(symbol, side, tp_adj, sl_adj, qty=qty, place_tp=not getattr(self, "dynamic_tp", False))
        return pos

    # --- 真实平仓（reduce-only 市价单） ---
    def _exit_fill(self, symbol, side, notional, bids, asks, price, pos=None):
        entry = pos.entry if pos else (price or 0.0)
        qty = notional / entry if entry else 0.0
        try:
            qty = float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            qty = notional / entry
        try:
            order = self.exchange.create_order(symbol, "market", side, qty, None, {"reduceOnly": True})
            fill = float(order.get("average") or order.get("price") or price or entry)
        except Exception as e:
            print(f"[live] 平仓下单失败 {symbol}: {e}")
            fill = price or entry
        # 平仓后取消残留的服务器端止盈止损条件单
        self._cancel_tp_sl(symbol)
        fee = self.fee
        return fill * (1 - fee) if side == "sell" else fill * (1 + fee)

    def _rest_wait(self, limit_per_min: int | None = None):
        """REST 限流（与 engine 共用同一思路，但 live 层独立维护，避免循环依赖）。"""
        # IP 封禁短路：engine 检测到封禁后，live 层也停止发 REST，避免续封
        eng = getattr(self, "engine", None)
        if eng is not None and getattr(eng, "_rest_banned", lambda: False)():
            raise RuntimeError("REST paused: IP banned")
        # live 层不实现完整滑动窗口，用简单 sleep 限流：每次 REST 前至少间隔 0.15s
        try:
            now = time.time()
            if hasattr(self, "_last_rest_ts"):
                dt = now - self._last_rest_ts
                if dt < 0.15:
                    time.sleep(0.15 - dt)
            self._last_rest_ts = time.time()
        except Exception:
            pass

    def _cancel_tp_sl(self, symbol):
        """取消该币所有挂起的止盈/止损 Algo 条件单（平仓后清理，避免残留单触发）。

        币安 Algo Order 用独立接口：fapiPrivateGetOpenAlgoOrders 查询、fapiPrivateDeleteAlgoOrder 取消。
        同时也兼容普通条件单（STOP_MARKET 等），双保险。
        """
        usym = symbol.replace("/USDT:USDT", "USDT").replace(":USDT", "")
        # 1) 取消 Algo 条件单
        try:
            self._rest_wait()
            algos = self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": usym})
            for a in algos:
                algo_id = a.get("algoId")
                if algo_id:
                    try:
                        self._rest_wait()
                        self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": str(algo_id), "symbol": usym})
                    except Exception:
                        pass
        except Exception:
            pass
        # 2) 取消普通条件单（兜底）
        try:
            self._rest_wait()
            opens = self.exchange.fetch_open_orders(symbol)
            for o in opens:
                has_stop = o.get("stopPrice") is not None or o.get("triggerPrice") is not None
                if has_stop:
                    try:
                        self._rest_wait()
                        self.exchange.cancel_order(o.get("id"), symbol)
                    except Exception:
                        pass
        except Exception:
            pass

    # --- 真实加仓（市价单） ---
    def average(self, symbol, side, add_price, add_size_usd, leverage, sl_dist=None, tp_dist=None):
        plist = self.positions.get(symbol) or []
        pos = None
        for p in reversed(plist):
            if p.side == side:
                pos = p
                break
        if not pos:
            return None
        self._prepare(symbol, leverage)
        qty = self._qty(symbol, add_size_usd * leverage, add_price)
        side_key = "buy" if side == "long" else "sell"
        try:
            order = self.exchange.create_order(symbol, "market", side_key, qty)
        except Exception as e:
            print(f"[live] 加仓下单失败 {symbol}: {e}")
            return None
        fill = float(order.get("average") or order.get("price") or add_price)
        r = self._apply_average(symbol, side, fill, add_size_usd, sl_dist, tp_dist)
        if r:
            print(f"[live] 加仓成功 {symbol} {side} qty={qty} 均价={r.entry:.8g}")
        return r

    # 限价挂单的成交 / 超时检查
    def settle_pending(self, symbol, bids=None, asks=None):
        meta = self.pending_entries.get(symbol)
        if not meta:
            return None
        order_id = meta["order_id"]
        try:
            order = self.exchange.fetch_order(order_id, symbol)
        except Exception:
            return None
        status = order.get("status")
        if status == "closed":
            fill = float(order.get("average") or order.get("price") or meta["entry"])
            side = meta["side"]
            del self.pending_entries[symbol]
            # 分批入场的限价半仓：成交后合并进已有持仓（加权平均）
            if meta.get("add"):
                r = self._apply_average(symbol, side, fill, meta["size_usd"],
                                        meta.get("sl_dist"), meta.get("tp_dist"))
                if r:
                    # 合并后按固定金额重算 TP/SL 并重挂服务器端条件单（名义变大 → 距离缩小）
                    self._recalc_sl_tp(r)
                    try:
                        self._cancel_tp_sl(symbol)
                    except Exception:
                        pass
                    self._place_tp_sl(symbol, r.side, r.tp, r.sl, place_tp=not getattr(self, "dynamic_tp", False))
                    print(f"[live] 限价半仓成交合并 {symbol} {side} @ {fill:.8g} 均价={r.entry:.8g}")
                return r
            sl_adj, tp_adj = self._adjust_sl_tp(side, meta["entry"], fill, meta["sl"], meta["tp"])
            pos = papermod.Position(symbol=symbol, side=side, entry=fill, size_usd=meta["size_usd"],
                                    leverage=meta["leverage"], sl=sl_adj, tp=tp_adj, highest=fill, lowest=fill,
                                    reversed=bool(meta.get("reversed", False)))
            self.positions.setdefault(symbol, []).append(pos)
            self._save()
            print(f"[live] 限价单成交开仓 {symbol} {side} @ {fill:.8g}")
            return pos
        if status in ("canceled", "expired"):
            del self.pending_entries[symbol]
            self._save()
            return "CANCELLED"
        # 挂单中：追踪建仓——价格朝有利方向走则撤旧单、追挂更优价
        if status == "open" and meta.get("add") and self.cfg.get("entry_chase_enabled", False):
            self._chase_limit(symbol, meta, bids, asks)
            order_id = meta["order_id"]   # 追价后可能换了新单 id
        timeout = float(self.cfg.get("limit_timeout_sec", 600))
        if time.time() - meta["created_at"] > timeout:
            try:
                self.exchange.cancel_order(order_id, symbol)
            except Exception:
                pass
            del self.pending_entries[symbol]
            self._save()
            print(f"[live] 限价单超时撤单 {symbol}")
            return "CANCELLED"
        return None

    def _chase_limit(self, symbol, meta, bids=None, asks=None):
        """追踪建仓：限价半仓单随价格朝有利方向移动，追挂更优价，避免顶部接盘。

        做多：价格下跌 → 撤旧买单，改挂到更低的盘口买一价（追更低，吃回踩）
        做空：价格上涨 → 撤旧卖单，改挂到更高的盘口卖一价
        约束：最多追到 entry±entry_chase_range_pct，且新旧价差≥entry_chase_min_step_pct 才改单。
        """
        side = meta["side"]
        entry = meta.get("entry") or 0.0
        if entry <= 0:
            return
        chase_range = float(self.cfg.get("entry_chase_range_pct", 1.0)) / 100
        min_step = float(self.cfg.get("entry_chase_min_step_pct", 0.05)) / 100
        old_price = meta.get("limit_price")
        if not old_price:
            return
        if side == "long":
            ref = (bids[0][0] if bids else None)
            if not ref:
                return
            floor = entry * (1 - chase_range)          # 最多追低到 entry-1%
            target = max(floor, ref)                    # 追到买一价，但不低于下限
            if target >= old_price:                     # 未更低，不追
                return
        else:
            ref = (asks[0][0] if asks else None)
            if not ref:
                return
            cap = entry * (1 + chase_range)             # 最多追高到 entry+1%
            target = min(cap, ref)
            if target <= old_price:
                return
        # 价差过小不追，避免频繁撤挂
        if abs(target - old_price) / old_price < min_step:
            return
        try:
            target = float(self.exchange.price_to_precision(symbol, target))
        except Exception:
            return
        try:
            self.exchange.cancel_order(meta["order_id"], symbol)
        except Exception as e:
            print(f"[live] 追踪建仓撤单失败 {symbol}: {e}")
            return
        try:
            side_key = "buy" if side == "long" else "sell"
            qty = self._qty(symbol, meta["size_usd"] * meta["leverage"], entry)
            self._rest_wait()
            no = self.exchange.create_order(symbol, "limit", side_key, qty, target)
            meta["order_id"] = no.get("id")
            meta["limit_price"] = target
            self._save()
            print(f"[live] 追踪建仓追价 {symbol} {side}: {old_price:.8g} → {target:.8g}")
        except Exception as e:
            print(f"[live] 追踪建仓重挂失败 {symbol}: {e}")

    def cancel_pending(self, symbol):
        meta = self.pending_entries.pop(symbol, None)
        if meta:
            try:
                self.exchange.cancel_order(meta["order_id"], symbol)
            except Exception:
                pass
            self._save()
            print(f"[live] 撤掉限价挂单 {symbol}")
        return None
