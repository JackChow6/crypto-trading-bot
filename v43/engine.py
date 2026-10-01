#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/engine.py —— V4.3 事件驱动双层 AI 交易引擎（编排器）

流水线：
  实时行情(ccxt REST 轮询 / 可升级 WebSocket)
    → Quant 特征 → Event Detector
    → 事件触发 → 本地小模型市场状态 → Opportunity Gate
    → API 大模型决策(LONG/SHORT/NO_TRADE)
    → Risk Engine → Paper Trade → Position Manager → Trade Journal

关键：只在「事件」发生时才唤醒 AI；小模型是观察员，大模型是决策者。

用法:
  python v43/engine.py --config config.yaml            # 实盘数据轮询
  python v43/engine.py --config config.yaml --simulate # 合成数据跑通全链路
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import threading
import time

import yaml
import ccxt

import features
import events
import ai
import risk as riskmod
import paper as papermod
import matching


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ecfg = cfg.get("engine") or {}
        self.symbol = self.ecfg.get("symbol", "BAT/USDT:USDT")
        self.timeframe = self.ecfg.get("timeframe", "5m")           # 主周期：方向 + 止盈幅度
        self.entry_timeframe = self.ecfg.get("entry_timeframe", "5m")  # 入场中周期确认（5m）
        self.fine_entry_timeframe = self.ecfg.get("fine_entry_timeframe", "1m")  # 入场精细定时（1m）
        self.sl_timeframe = self.ecfg.get("sl_timeframe", "1h")     # 止损周期：动态止损用更高周期 ATR
        self.poll = int(self.ecfg.get("poll_interval", 60))
        self.cooldown = int(self.ecfg.get("cooldown", 180))  # 事件冷却，避免重复触发
        self.local_cfg = self.ecfg.get("local_ai") or {}
        self.risk = riskmod.RiskEngine(self.ecfg.get("risk") or {})
        self.matcher = matching.MatchingEngine()
        self.paper = papermod.PaperTrader(self.ecfg.get("paper") or {}, self.matcher)
        # 同步风险状态与从磁盘恢复的模拟盘
        self.risk.state.equity = self.paper.equity
        self.risk.state.peak_equity = self.paper.peak_equity
        self.risk.state.open_positions = sum(len(ps) for ps in self.paper.positions.values())
        self.last_event_ts = 0.0
        self.last_event_ts_by_sym: dict[str, float] = {}   # 每个币独立的事件冷却时间戳
        self.exchange = None   # 懒加载：run() 时才建；simulate 用不到
        self.watchlist_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
        self.watchlist: dict[str, dict] = {}   # 延迟加载：TG 模式从磁盘恢复，涨幅榜模式直接拉取最新榜单
        self.oi_history: dict[str, list] = {}  # symbol -> [(ts, oi), ...]，用于计算 5m OI 变化
        self.oi_cache: dict[str, tuple[float, float]] = {}   # symbol -> (ts, oi)，REST OI 缓存，避免每轮打爆限流
        self.tf_fail: dict = {}   # (symbol, tf) -> 上次失败时间，失败退避避免 429 重试雪崩
        self._rest_lock = threading.Lock()
        self._rest_window: list[float] = []   # REST 请求时间戳滑动窗口（60s），用于 IP 限流
        self._rest_banned_until: float = 0.0   # 币安 IP 封禁截止时间戳（封禁期内停止发 REST，避免续封）
        self.trailing_cfg = self.ecfg.get("trailing") or {}
        self.dash = None   # Dashboard 实例（可选）
        self.candles_cache: dict[str, list] = {}   # symbol -> 最近 1m K线，供 dashboard 绘图
        self.tf_cache: dict = {}   # (symbol, timeframe) -> (ts, candles)，多周期按需缓存
        self.last_prices: dict[str, float] = {}   # symbol -> 最新价(REST)，供 dashboard 浮盈计算
        self.atr_cache: dict[str, float] = {}     # symbol -> 最新 ATR，供快速结算用
        self.sl_atr_cache: dict[str, tuple[float, float]] = {}   # symbol -> (ts, 止损周期ATR)，供动态止损用
        self.pending_confirm: dict[str, dict] = {}   # symbol -> 待 1m 反转确认的入场单
        self.snap_cache: dict[str, dict] = {}     # symbol -> 最近一次市场快照，供 AI 复盘用
        self.last_ai_review: dict[str, float] = {}   # symbol -> 上次 AI 复盘时间戳
        self.decision_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision_log.jsonl")
        self.equity_history: list[list] = []      # [[ts, equity], ...] 权益曲线
        self.decision_history: list[dict] = self._load_history_decisions()   # 最近 AI 决策（含历史回填）
        self.history_trades: list[dict] = self._load_history_trades()   # 从决策日志回填的历史成交
        self.gainer_board: list[dict] = []   # 最近一次动量/涨幅榜完整榜单（含 change_pct/rets/quote_volume），供 dashboard 展示

    def _read_tail_lines(self, path: str, n: int) -> list[str]:
        """只读文件尾部 n 行（决策日志会持续增长，全量读浪费内存）。"""
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # 从尾部读，最多读 1MB（足够覆盖 n 行）
                read_size = min(size, 1024 * 1024)
                f.seek(size - read_size)
                data = f.read().decode("utf-8", errors="ignore")
            return data.splitlines()[-n:]
        except Exception:
            return []

    def _load_history_decisions(self) -> list[dict]:
        """从 decision_log.jsonl 读取历史决策(decision 行)，供 dashboard 只读展示。"""
        decs = []
        for line in self._read_tail_lines(self.decision_log_file, 200):
            try:
                rec = json.loads(line.strip())
            except Exception:
                continue
            if rec.get("type") == "decision":
                decs.append(rec)
        return decs[-20:]

    def _load_history_trades(self) -> list[dict]:
        """从 decision_log.jsonl 读取历史平仓记录(outcome 行)，供 dashboard 只读展示。"""
        trades = []
        for line in self._read_tail_lines(self.decision_log_file, 500):
            try:
                rec = json.loads(line.strip())
            except Exception:
                continue
            if rec.get("type") == "outcome":
                trades.append({
                    "symbol": rec.get("symbol", ""),
                    "side": rec.get("side", ""),
                    "entry": rec.get("entry"),
                    "exit": rec.get("exit"),
                    "pnl": rec.get("pnl", 0),
                    "reason": rec.get("reason", ""),
                    "ts": rec.get("ts", 0),
                })
        return trades

    def _load_watchlist(self) -> dict:
        try:
            with open(self.watchlist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    print(f"[engine] 从磁盘恢复 watchlist：{len(data)} 个币")
                    return data
        except Exception:
            pass
        return {}

    def _save_watchlist(self):
        try:
            with open(self.watchlist_file, "w", encoding="utf-8") as f:
                json.dump(self.watchlist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[engine] 保存 watchlist 失败: {e}")

    def _build_exchange(self):
        xc = self.cfg["exchange"]
        params = {"enableRateLimit": True,
                  "options": {"defaultType": xc.get("default_type", "swap"),
                              # 合约交易不需要币种信息，跳过 fetch_currencies 避免现货域名 api.binance.com 网络抖动导致启动失败
                              "fetchCurrencies": False}}
        if xc.get("api_key"):
            params["apiKey"] = xc["api_key"]
            params["secret"] = xc["api_secret"]
        if xc.get("proxy"):
            params["proxies"] = {"http": xc["proxy"], "https": xc["proxy"]}
        ex = getattr(ccxt, xc["name"])(params)
        if xc.get("sandbox"):
            ex.set_sandbox_mode(True)
        try:
            ex.load_markets()
        except Exception as e:
            # load_markets 失败不阻断（可能只是 currencies 等非必需接口失败），
            # 引擎实际下单用 fetch_ohlcv/fetch_tickers 等合约接口，不受影响。
            print(f"[engine] load_markets 失败（忽略）: {e}")
        return ex

    def _setup_trader(self):
        """实盘模式：用 LiveTrader（真实下单）替换模拟盘，并同步风控权益状态。"""
        if self.ecfg.get("live", False):
            import live as livemod
            self.paper = livemod.LiveTrader(self.ecfg.get("paper") or {}, self.exchange)
            self.paper.dynamic_tp = bool(self.ecfg.get("dynamic_tp", False))
            self.paper.fixed_tp_usd = float(self.ecfg.get("fixed_tp_usd", 0.0) or 0.0)
            self.paper.fixed_sl_usd = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
            # 动态止损参数透传给实盘层（限价半仓合并后重算 SL 时用）
            self.paper.dynamic_sl = bool(self.ecfg.get("dynamic_sl", False))
            self.paper.sl_atr_mult = float(self.ecfg.get("dyn_sl_atr_mult", 2.0))
            self.paper.sl_min_usd = float(self.ecfg.get("sl_min_usd", 0.0) or 0.0)
            self.paper.sl_max_usd = float(self.ecfg.get("sl_max_usd", 0.0) or 0.0)
            self.paper.atr_cache = self.atr_cache   # 共享引用，动态止损读当前 ATR
            self.paper.sl_atr_fn = self._sl_atr     # 止损周期 ATR 回调（限价半仓合并后重算 SL 用）
            self.paper.engine = self   # 注入 engine 引用：live 层 REST 前查询封禁状态，封禁期跳过避免续封
            self.risk.state.equity = self.paper.equity
            self.risk.state.peak_equity = self.paper.peak_equity
            self.risk.state.open_positions = sum(len(ps) for ps in self.paper.positions.values())
            # 固定止盈止损：对已恢复的持仓也按固定金额重设 SL/TP
            self._apply_fixed_sl_tp()

    def _apply_roi_decay(self, plist, fixed_tp):
        """时间衰减止盈：持仓越久，止盈门槛越低（freqtrade minimal_roi 思路）。

        持仓 < mid_min 分钟：止盈 = fixed_tp_usd（正常）
        持仓 mid_min~min_min 分钟：止盈 = roi_decay_mid_usd
        持仓 > min_min 分钟：止盈 = roi_decay_min_usd（保本微利）

        目的：持仓久的单（尤其浮亏死扛的）尽早离场，释放仓位名额接新信号。
        只调低止盈价（朝更早止盈方向），不抬高。
        """
        mid_min = float(self.ecfg.get("roi_decay_mid_min", 60))
        mid_usd = float(self.ecfg.get("roi_decay_mid_usd", 1.5))
        min_min = float(self.ecfg.get("roi_decay_min_min", 180))
        min_usd = float(self.ecfg.get("roi_decay_min_usd", 0.5))
        now = time.time()
        for pos in plist:
            held_min = (now - pos.opened_at) / 60.0
            if held_min <= mid_min:
                roi_usd = fixed_tp
            elif held_min <= min_min:
                roi_usd = mid_usd
            else:
                roi_usd = min_usd
            notional = pos.size_usd * pos.leverage
            if notional <= 0:
                continue
            tp_dist = pos.entry * (roi_usd / notional)
            if pos.side == "long":
                new_tp = pos.entry + tp_dist
                # 只调低止盈（更早止盈），不抬高
                if pos.tp is None or new_tp < pos.tp:
                    pos.tp = round(new_tp, 8)
            else:
                new_tp = pos.entry - tp_dist
                if pos.tp is None or new_tp > pos.tp:
                    pos.tp = round(new_tp, 8)

    def _apply_sl_decay(self, plist):
        """时间衰减止损：持仓越久，止损越紧（止损价向 entry 靠拢）。

        持仓 < mid_min 分钟：止损 = 原始动态止损（宽，给空间）
        持仓 mid_min~min_min 分钟：止损收紧到 sl_decay_mid_usd
        持仓 > min_min 分钟：止损收紧到 sl_decay_min_usd（几乎贴着 entry，死扛单尽早离场）

        只收紧（止损价朝 entry 方向移动），不放松。
        """
        mid_min = float(self.ecfg.get("sl_decay_mid_min", 120))
        mid_usd = float(self.ecfg.get("sl_decay_mid_usd", 1.5))
        min_min = float(self.ecfg.get("sl_decay_min_min", 240))
        min_usd = float(self.ecfg.get("sl_decay_min_usd", 0.5))
        now = time.time()
        for pos in plist:
            held_min = (now - pos.opened_at) / 60.0
            if held_min <= mid_min:
                continue   # 还没到衰减时间，保持原始止损
            elif held_min <= min_min:
                sl_usd = mid_usd
            else:
                sl_usd = min_usd
            notional = pos.size_usd * pos.leverage
            if notional <= 0:
                continue
            sl_dist = pos.entry * (sl_usd / notional)
            if pos.side == "long":
                new_sl = pos.entry - sl_dist
                # 只收紧（止损上移），不放松
                if pos.sl is None or new_sl > pos.sl:
                    pos.sl = round(new_sl, 8)
            else:
                new_sl = pos.entry + sl_dist
                # 做空止损收紧 = 止损价下移
                if pos.sl is None or new_sl < pos.sl:
                    pos.sl = round(new_sl, 8)

    def _dynamic_sl_usd(self, sym, entry, atr, notional):
        """动态止损金额（USDT）：按 ATR 波动率自适应。

        止损金额 = ATR 价格距离 × sl_atr_mult 折算成 USDT（= atr × mult × notional / entry），
        再限制在 [sl_min_usd, sl_max_usd] 区间。高波动币止损放宽、低波动币收紧，
        避免固定金额在波动大时被噪音震出、波动小时又扛过多亏损。
        ATR 缺失或 dynamic_sl=false 时，回退到固定 fixed_sl_usd。
        """
        fixed = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
        if not self.ecfg.get("dynamic_sl", False):
            return fixed
        if atr and notional and entry:
            sl_usd = atr * float(self.ecfg.get("dyn_sl_atr_mult", 2.0)) * notional / entry
        else:
            sl_usd = fixed
        sl_min = float(self.ecfg.get("sl_min_usd", 0.0) or 0.0)
        sl_max = float(self.ecfg.get("sl_max_usd", 0.0) or 0.0)
        if sl_min > 0:
            sl_usd = max(sl_usd, sl_min)
        if sl_max > 0:
            sl_usd = min(sl_usd, sl_max)
        return sl_usd

    def _apply_fixed_sl_tp(self):
        """按 fixed_tp_usd / fixed_sl_usd 以「币种」为单位重设止盈止损。

        加仓合并后该币种总名义金额变大，固定 2u/1.5u 对应的价格距离相应缩小。
        实盘模式下同时重挂币安服务器端条件单（先取消旧的，再按新价挂），
        保证服务器端止盈止损与本地一致，且引擎断线也能按最新价触发。
        """
        fixed_tp = float(self.ecfg.get("fixed_tp_usd", 0.0) or 0.0)
        fixed_sl = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
        if fixed_tp <= 0 or fixed_sl <= 0:
            return
        is_live = self.ecfg.get("live", False)
        dynamic_tp = bool(self.ecfg.get("dynamic_tp", False))
        changed = 0
        for sym, plist in self.paper.positions.items():
            for pos in plist:
                notional = pos.size_usd * pos.leverage
                if notional <= 0:
                    continue
                tp_dist = pos.entry * (fixed_tp / notional)
                # 动态止损：按该币当前 ATR 自适应金额（ATR 缺失回退固定值）
                sl_usd = self._dynamic_sl_usd(sym, pos.entry, self._sl_atr(sym), notional)
                sl_dist = pos.entry * (sl_usd / notional)
                if pos.side == "long":
                    pos.sl = round(pos.entry - sl_dist, 8)
                    pos.tp = round(pos.entry + tp_dist, 8)
                else:
                    pos.sl = round(pos.entry + sl_dist, 8)
                    pos.tp = round(pos.entry - tp_dist, 8)
                changed += 1
            # 实盘：重挂该币种的服务器端止盈止损条件单（以最后一笔仓位为准）
            if is_live and plist and hasattr(self.paper, "_cancel_tp_sl") and hasattr(self.paper, "_place_tp_sl"):
                last = plist[-1]
                try:
                    self.paper._cancel_tp_sl(sym)
                except Exception:
                    pass
                try:
                    # 动态止盈：服务器端只挂止损、不挂固定止盈（触及目标由本地锁利并继续奔跑）
                    self.paper._place_tp_sl(sym, last.side, last.tp, last.sl, place_tp=not dynamic_tp)
                except Exception as e:
                    print(f"[fixed] 重挂 {sym} 条件单失败: {e}")
        if changed:
            self.paper._save()
            mode = "动态止盈(仅止损)+移动止损" if dynamic_tp else "固定止盈/止损"
            print(f"[fixed] 已按{fixed_tp}u/{fixed_sl}u 重设 {changed} 笔持仓（模式：{mode}）")

    # --- 数据采集 ---
    def fetch_raw(self, symbol: str | None = None) -> dict:
        sym = symbol or self.symbol
        raw = {"symbol": sym}
        try:
            self._rest_wait()
            raw["ohlcv"] = self.exchange.fetch_ohlcv(sym, self.timeframe, limit=120)
        except Exception as e:
            self._note_rest_ban(str(e))
            raw["ohlcv"] = []
        try:
            self._rest_wait()
            raw["orderbook"] = self.exchange.fetch_order_book(sym, 10)
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        try:
            self._rest_wait()
            t = self.exchange.fetch_ticker(sym)
            raw["last"] = t.get("last")     # 实时最新价（比 1m 收盘更准，用于入场/结算）
            raw["funding"] = t.get("info", {}).get("fundingRate") or t.get("info", {}).get("lastFundingRate")
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        # 订单流：逐笔成交（计算 Delta/CVD 用）
        try:
            self._rest_wait()
            raw["trades"] = self.exchange.fetch_trades(sym, limit=500)
        except Exception as e:
            self._note_rest_ban(str(e))
            raw["trades"] = []
        # OI：ccxt 各所接口不同，尽量尝试；并维护历史计算 5m 变化（带缓存避免限流）
        raw["oi"] = self._fetch_oi(sym)
        raw["oi_change_5m_pct"] = self._oi_change_5m(sym, raw.get("oi"))
        return raw

    def _fetch_oi(self, sym: str):
        """带缓存地拉取 OI（300s TTL），避免每轮都打 REST 触发币安 IP 限流。"""
        now = time.time()
        hit = self.oi_cache.get(sym)
        if hit and now - hit[0] < 300:
            return hit[1]
        oi = None
        try:
            self._rest_wait()
            oi = self.exchange.fetch_open_interest(sym).get("openInterestAmount")
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        self.oi_cache[sym] = (now, oi)
        return oi

    # --- 全局 REST 限流：把 REST 调用总量压到币安 IP 限流(2400/min)以内 ---
    def _note_rest_ban(self, msg: str):
        """识别币安 IP 封禁错误（-1003 banned until ...），记录封禁截止时间。

        封禁期内所有 REST 调用直接短路，不再发请求——继续发请求会让封禁时间不断延长。
        """
        import re
        m = re.search(r"banned until (\d{13})", msg or "")
        if m:
            try:
                self._rest_banned_until = max(self._rest_banned_until, int(m.group(1)) / 1000.0)
                print(f"[engine] 检测到 IP 封禁，REST 暂停至 {time.strftime('%H:%M:%S', time.localtime(self._rest_banned_until))}")
            except Exception:
                pass

    def _rest_banned(self) -> bool:
        return time.time() < self._rest_banned_until

    def _rest_wait(self, limit_per_min: int | None = None):
        """滑动窗口限流 + IP 封禁短路：保证最近 60s 内的 REST 调用数不超过上限，超出则 sleep 到窗口滑出。"""
        if self._rest_banned():
            # 封禁期内不发 REST：抛异常让上层走缓存/降级路径
            raise RuntimeError("REST paused: IP banned")
        limit = limit_per_min or int(self.ecfg.get("rest_rate_limit", 1500))
        now = time.time()
        with self._rest_lock:
            w = self._rest_window
            # 清理 60s 之前的记录
            w[:] = [t for t in w if now - t < 60.0]
            if len(w) >= limit:
                # 等到最早一条滑出窗口
                sleep_for = w[0] + 60.0 - now + 0.02
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                w[:] = [t for t in w if now - t < 60.0]
            w.append(now)

    def _oi_change_5m(self, sym: str, oi):
        """维护 OI 历史，返回当前 OI 相对 ~5 分钟前的百分比变化。"""
        if oi is None:
            return None
        try:
            oi = float(oi)
        except (TypeError, ValueError):
            return None
        hist = self.oi_history.setdefault(sym, [])
        now = time.time()
        hist.append((now, oi))
        hist = [x for x in hist if now - x[0] <= 3600]  # 只留 1 小时内
        self.oi_history[sym] = hist
        for ts, v in hist:
            if now - ts >= 240:   # 找 ≥4 分钟前的最近读数
                if v:
                    return round((oi / v - 1) * 100, 4)
                return 0.0
        return 0.0

    def fetch_candles(self, sym: str, timeframe: str | None = None, limit: int = 120) -> list:
        """按需拉取 K线（默认用交易周期，1m 缓存 10s，其它周期缓存 60s），供 dashboard 绘图。"""
        tf = timeframe or self.timeframe
        # 交易所没有该币（如切换交易所后遗留的旧仓位），直接返回空
        if self.exchange is not None and getattr(self.exchange, "markets", None) is not None and sym not in self.exchange.markets:
            return []
        key = (sym, tf)
        now = time.time()
        # 按周期设置缓存 TTL：K 线周期越长，越没必要频繁重拉（省 REST、避限流）
        ttl_map = {"1m": 10, "5m": 60, "15m": 180, "1h": 300, "4h": 600, "1d": 3600}
        ttl = ttl_map.get(tf, 120)
        if key in self.tf_cache and now - self.tf_cache[key][0] < ttl:
            return self.tf_cache[key][1]
        # 失败退避：刚失败过（429 限流等）在退避期内直接返回缓存，不重试，避免打爆限流
        if key in self.tf_fail and now - self.tf_fail[key] < ttl:
            return self.tf_cache.get(key, (0, []))[1]
        try:
            self._rest_wait()
            ohlcv = self.exchange.fetch_ohlcv(sym, tf, limit=limit)
            self.tf_cache[key] = (now, ohlcv)
            self.tf_fail.pop(key, None)
            return ohlcv
        except Exception as e:
            self.tf_fail[key] = now
            self._note_rest_ban(str(e))
            print(f"[engine] 拉取 {tf} K线失败 {sym}: {e}")
            return self.tf_cache.get(key, (0, []))[1]

    def _ma99_4h_trend(self, sym: str):
        """4h 周期 MA99 趋势（约 16 天大趋势）。

        用 4h K 线算 99 根收盘均价，收盘价 > MA99 = LONG（多头趋势），< MA99 = SHORT（空头趋势）。
        数据不足返回 None（让上层跳过 MA99 判断）。
        """
        try:
            candles = self.fetch_candles(sym, "4h", limit=110)
            if not candles or len(candles) < 99:
                return None
            closes = [c[4] for c in candles]
            ma = sum(closes[-99:]) / 99
            return "LONG" if closes[-1] > ma else "SHORT"
        except Exception:
            return None

    def _sl_atr(self, sym: str) -> float:
        """止损周期 ATR（多时间框架止损）。

        用 sl_timeframe（默认 1h）的 K 线计算 ATR(14)，反映更大级别的波动，
        避免动态止损被 15m 主周期的短周期噪音震出。带缓存（TTL 随周期加长）。
        失败或数据不足时回退到主周期 atr_cache，再回退 0。
        """
        # 固定止损模式（dynamic_sl=false）不需要 1h ATR，直接返回 0，省掉一次 REST（避免 IP 限流）
        if not self.ecfg.get("dynamic_sl", False):
            return 0.0
        tf = self.sl_timeframe
        cached = self.sl_atr_cache.get(sym)
        ttl = {"1m": 10, "5m": 60, "15m": 180, "1h": 300, "4h": 600, "1d": 3600}.get(tf, 300)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        atr_val = 0.0
        try:
            candles = self.fetch_candles(sym, tf, limit=60)
            if candles:
                atr_val = features.atr(candles, 14) or 0.0
        except Exception:
            pass
        if atr_val <= 0:
            atr_val = self.atr_cache.get(sym, 0.0)   # 回退主周期 ATR
        self.sl_atr_cache[sym] = (time.time(), atr_val)
        return atr_val

    def _stream_price(self, sym: str):
        """从行情流取最新价（兼容 dict[MarketStream] 与 MultiStream 两种形态）。

        超过 30 秒未更新的 WS 价视为过期，返回 None 让上层回退 REST，
        避免 WS 断流时浮盈/结算用陈旧价格卡住不动。
        """
        st = getattr(self, "streams", None)
        if st is None:
            return None
        if isinstance(st, dict):
            ms = st.get(sym)
            if ms is not None and getattr(ms, "healthy", False) and getattr(ms, "last_price", None):
                return ms.last_price
            return None
        if getattr(st, "healthy", False):
            px = st.last_price.get(sym)
            if px:
                ts = getattr(st, "last_price_ts", {}).get(sym, 0)
                if time.time() - ts < 30:
                    return px
        return None

    def _stream_book(self, sym: str):
        """从行情流取盘口 (bids, asks)，拿不到返回空。"""
        st = getattr(self, "streams", None)
        if st is None:
            return [], []
        if isinstance(st, dict):
            ms = st.get(sym)
            if ms is not None and getattr(ms, "healthy", False):
                ob = ms.orderbook or {}
                return ob.get("bids", []), ob.get("asks", [])
            return [], []
        if getattr(st, "healthy", False):
            ob = st.orderbooks.get(sym) or {}
            return ob.get("bids", []), ob.get("asks", [])
        return [], []

    def _stream_snapshot(self, sym: str):
        """从行情流取完整快照；无 K 线时返回 None 让上层走 REST 回退。"""
        st = getattr(self, "streams", None)
        if st is None:
            return None
        if isinstance(st, dict):
            ms = st.get(sym)
            if ms is not None and getattr(ms, "healthy", False):
                raw = ms.snapshot()
                return raw if raw.get("ohlcv") else None
            return None
        if getattr(st, "healthy", False):
            raw = st.snapshot(sym)
            return raw if raw.get("ohlcv") else None
        return None

    def fetch_price(self, sym: str):
        """实时最新价：优先用 WebSocket 流，否则 REST 拉取（5s 缓存）。"""
        px = self._stream_price(sym)
        if px:
            return px
        key = ("price", sym)
        now = time.time()
        if key in self.tf_cache and now - self.tf_cache[key][0] < 5:
            return self.tf_cache[key][1]
        px = self.last_prices.get(sym)
        try:
            self._rest_wait()
            t = self.exchange.fetch_ticker(sym)
            px = t.get("last")
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        self.tf_cache[key] = (now, px)
        return px

    def _entry_price(self, sym: str, fallback: float):
        """用入场周期 K 线定入场价：取最近一根 entry_timeframe K 线收盘价（更贴近实际入场点），拿不到回退快照价。"""
        try:
            candles = self.fetch_candles(sym, self.entry_timeframe, limit=5)
            if candles:
                return float(candles[-1][4])
        except Exception:
            pass
        return fallback

    def _flow_confirm(self, sym: str, is_long: bool) -> bool:
        """量价 + 订单流确认：做多看买方资金回归，做空看卖方资金回归。

        数据完全缺失时放行（退化为纯 K 线确认，不卡死入场）。
        """
        snap = self.snap_cache.get(sym) or {}
        flow = snap.get("orderflow") or {}
        vol = snap.get("volume") or {}
        tbr = flow.get("taker_buy_ratio")
        cvd_trend = flow.get("cvd_trend")
        delta5 = flow.get("delta_5m")
        vol_ratio = snap.get("vol_ratio")
        vol_trend = vol.get("trend")
        vol_spike = bool(vol.get("spike"))
        tbr_thr = float(self.ecfg.get("flow_taker_buy_ratio", 0.55))
        # 资金方向回归：至少满足其一（taker 占比 / CVD 趋势 / 5m delta）
        has_flow = tbr is not None or cvd_trend or delta5 is not None
        if has_flow:
            if is_long:
                flow_ok = (tbr is not None and tbr >= tbr_thr) or cvd_trend == "RISING" or \
                          (delta5 is not None and delta5 > 0)
            else:
                flow_ok = (tbr is not None and tbr <= 1 - tbr_thr) or cvd_trend == "FALLING" or \
                          (delta5 is not None and delta5 < 0)
            if not flow_ok:
                return False
        # 量能配合：放量更可信；数据缺失时放行，缩量不阻断（缩量回踩本身健康）
        if vol_ratio is not None and vol_ratio < 1.0 and vol_trend == "DECREASING" and not vol_spike:
            return False
        return True

    def _check_pending_confirm(self, sym: str):
        """量价 + 订单流确认入场：信号进 pending_confirm 后，只等资金流回归（不做 K 线反转确认），
        满足即市价入场。做多看买方回归、做空看卖方回归，数据缺失放行。
        """
        meta = self.pending_confirm.get(sym)
        if not meta:
            return None
        # 超时未确认 → 撤单，释放仓位名额
        timeout = float(self.ecfg.get("confirm_timeout", 15)) * 60
        if time.time() - meta["created_at"] > timeout:
            self.pending_confirm.pop(sym, None)
            self.risk.on_order_cancelled()
            print(f"[确认] {sym} 超时未确认，撤单")
            return "CANCELLED"
        is_long = meta["side"] == "long"
        # 量价 + 订单流确认（买方/卖方资金回归），满足才入场；
        # 反向跟单的单子跳过资金流确认（否则确认方向与反向方向矛盾，会卡到超时）
        if not meta.get("reverse") and not self._flow_confirm(sym, is_long):
            return None
        # 用实时价市价入场，盘口取最新
        px = self.fetch_price(sym) or meta.get("entry_ref") or self.last_prices.get(sym)
        if px is None:
            return None
        bids, asks = self._stream_book(sym)
        try:
            self._rest_wait()
            fresh = self.exchange.fetch_order_book(sym, 10)
            if fresh and fresh.get("bids") and fresh.get("asks"):
                bids, asks = fresh.get("bids", []), fresh.get("asks", [])
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        res = self.paper.open(sym, meta["side"], px, meta["size_usd"], meta["leverage"],
                              meta["sl"], meta["tp"], bids=bids, asks=asks,
                              reversed=bool(meta.get("reversed", False)))
        self.pending_confirm.pop(sym, None)
        if res is None:
            self.risk.on_order_cancelled()   # 确认后仍失败（重复），释放名额
            print(f"[确认] {sym} 资金流确认但开仓失败(重复)，撤单")
            return "FAILED"
        print(f"[确认] {sym} {meta['side']} 量价+订单流确认，市价入场 @ {px}")
        return res

    # --- 单步流水线 ---
    def step(self, raw: dict, signal: dict | None = None) -> dict:
        snap = features.build_snapshot(raw)
        if not snap:
            return {"event": None, "decision": None}
        evs = events.detect(snap, self.ecfg.get("events") or {})
        # 入场触发过滤：pullback=只回踩 / breakdown=只跌破 / breakout=只突破跌破 / both=回踩+跌破
        trigger = self.ecfg.get("entry_trigger", "any")
        if trigger == "pullback":
            evs = [e for e in evs if e.type == "PULLBACK"]
        elif trigger == "breakdown":
            evs = [e for e in evs if e.type == "BREAKDOWN"]
        elif trigger == "breakout":
            evs = [e for e in evs if e.type in ("BREAKOUT", "BREAKDOWN")]
        elif trigger == "both":
            evs = [e for e in evs if e.type in ("PULLBACK", "BREAKDOWN")]
        result = {"snapshot": snap, "events": [e.type for e in evs], "decision": None}

        if not evs:
            return result
        # 事件冷却：每个币独立计时，避免同一币的同一事件反复触发，但不挡其它币
        sym_key = snap.get("symbol") or self.symbol
        now = time.time()
        # 清理过期冷却记录，避免字典无限增长（超过 2×cooldown 未触发的币清除）
        stale = [k for k, ts in self.last_event_ts_by_sym.items() if now - ts > self.cooldown * 2]
        for k in stale:
            self.last_event_ts_by_sym.pop(k, None)
        if now - self.last_event_ts_by_sym.get(sym_key, 0.0) < self.cooldown:
            return result
        self.last_event_ts_by_sym[sym_key] = now

        # 信号方向：跟单 TG 给出的 buy/sell（both=无固定方向，多空都做）
        sig_side = None
        sig_ctx = None
        if signal:
            s = (signal.get("side") or "").lower()
            if s == "buy":
                sig_side = "LONG"
            elif s == "sell":
                sig_side = "SHORT"
            if sig_side:
                sig_ctx = {"direction": sig_side, "entry_ref": signal.get("entry_ref"),
                           "sl_ref": signal.get("sl_ref"), "tp_ref": signal.get("tp_ref")}

        # 直接决策：不经过观察员/机会门，只保留一个确定性 RSI 极值过滤（避免明显追涨杀跌）
        rsi = snap.get("rsi14")
        ev_types = [e.type for e in evs]
        long_ev = any(t in ("PULLBACK", "BREAKOUT") for t in ev_types)
        short_ev = any(t == "BREAKDOWN" for t in ev_types)
        if long_ev and not short_ev and rsi is not None and rsi >= 80:
            result["gate"] = f"RSI_OVERBOUGHT({rsi:.0f})"
            return result
        if short_ev and not long_ev and rsi is not None and rsi <= 20:
            result["gate"] = f"RSI_OVERSOLD({rsi:.0f})"
            return result
        result["gate"] = "DIRECT"

        # 3) 本地大模型：最终决策（跟随信号方向 + 最近交易复盘）
        positions = [{"symbol": s, "side": p.side, "entry": p.entry,
                      "pnl": round(p.unrealized_pnl(snap["price"]), 2)}
                     for s, ps in self.paper.positions.items() for p in ps]
        risk_state = {"equity": round(self.risk.state.equity, 2),
                      "open_positions": self.risk.state.open_positions,
                      "daily_pnl": round(self.risk.state.daily_pnl, 2)}
        recent_trades = [{"symbol": t.symbol, "side": t.side, "pnl": t.pnl, "reason": t.exit_reason}
                         for t in self.paper.trades[-5:]]
        # 复盘闭环：聚合最近交易表现，喂给模型（胜率/净盈亏/亏损币种/亏损原因）
        tr20 = self.paper.trades[-20:]
        wins = [t for t in tr20 if t.pnl > 0]
        losses = [t for t in tr20 if t.pnl <= 0]
        reflection = {
            "recent_5": recent_trades,
            "summary": {
                "win_rate": round(len(wins) / len(tr20) * 100, 1) if tr20 else 0.0,
                "net_pnl": round(sum(t.pnl for t in tr20), 2),
                "losing_symbols": sorted(set(t.symbol for t in losses))[:6],
                "losing_reasons": sorted(set(t.exit_reason for t in losses)),
            },
        }
        # 信号源：确定性量化信号（rule）或本地大模型（ai）
        signal_source = str(self.ecfg.get("signal_source", "ai")).lower()
        if signal_source == "rule":
            import rule_signal
            d = rule_signal.decide(snap, evs, self.ecfg.get("rule_signal") or {})
        else:
            d = ai.decision(self.local_cfg, evs, snap, positions, risk_state,
                            signal=sig_ctx, recent_trades=reflection)
        result["decision"] = {"action": d.action, "confidence": d.confidence, "reason": d.reason,
                              "risk_level": d.risk_level, "bull": d.bull, "bear": d.bear}

        # 大模型不可用时的确定性兜底（仍走 Risk Engine）
        if d.action == "NO_TRADE" and d.reason.startswith("决策模型调用失败") and self.ecfg.get("rule_fallback", True):
            d = self._rule_decision(snap, state, setup)

        # 硬性跟单：信号方向明确时，AI 若给出反向则强制纠正为信号方向
        if sig_side and d.action in ("LONG", "SHORT") and d.action != sig_side:
            d.action = sig_side
            d.sl = None
            d.tp = None
            d.reason = f"已按信号方向纠正为 {sig_side}。{d.reason}"[:200]

        # 方向决策：
        # 1) ma99_short_signal：窄信号模式——只做「共振做多 + 4h MA99空 → 反向做空」，其他一律不开单
        # 2) reverse：无条件反向跟单（镜像信号方向）
        reversed_dir = False
        if self.ecfg.get("ma99_short_signal", False) and d.action in ("LONG", "SHORT"):
            ma = self._ma99_4h_trend(snap.get("symbol") or self.symbol)
            if d.action == "LONG" and ma == "SHORT":
                # 共振做多 + 4h MA99空 → 反向做空
                d.action = "SHORT"
                reversed_dir = True
                d.reason = f"[MA99空反空] {d.reason}"[:200]
            else:
                result["gate"] = "MA99_SHORT_FILTER"
                return result
        elif self.ecfg.get("reverse", False) and d.action in ("LONG", "SHORT"):
            d.action = "LONG" if d.action == "SHORT" else "SHORT"
            reversed_dir = True
            d.reason = f"[反向] {d.reason}"[:200]

        # 信号过滤（基于特征分析的实证结论）：黑名单拒绝 + delta_1m/macd 逆势盘过滤
        if d.action in ("LONG", "SHORT") and self.ecfg.get("signal_filter_enabled", True):
            import signal_filter
            fcfg = dict(self.ecfg.get("signal_filter") or {})
            fcfg["trades"] = self.paper.trades   # 传入实盘成交，动态黑名单实时更新
            ok, why = signal_filter.filter_signal(d, snap, fcfg)
            if not ok:
                result["gate"] = f"FILTER:{why}"
                return result

        # 只做空：最终方向为 LONG 的直接拒绝（只允许 SHORT）
        if self.ecfg.get("short_only", False) and d.action == "LONG":
            result["gate"] = "SHORT_ONLY_REJECT"
            return result

        # 4) 确定性入场/止盈/止损
        # 入场价用 1 分钟 K 线收盘价定（更贴近实际入场点），拿不到回退 5m 快照价
        entry = self._entry_price(snap.get("symbol") or self.symbol, snap["price"])
        is_long = d.action == "LONG"
        fixed_tp = float(self.ecfg.get("fixed_tp_usd", 0.0) or 0.0)
        fixed_sl = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
        # 每笔名义金额 = 保证金 × 杠杆（fixed 模式下由 risk 配置确定）
        size_usd = float(self.ecfg.get("risk", {}).get("max_position_usd", 5.0))
        leverage = float(self.ecfg.get("risk", {}).get("leverage", 20))
        notional = size_usd * leverage
        atr = snap.get("atr") or 0.0   # risk.check 需要 atr 参数，固定模式下仅作风险估算
        if fixed_tp > 0 and fixed_sl > 0 and notional > 0:
            # 固定止盈 + 动态止损：止盈按固定金额换算；止损按高周期 ATR 波动率自适应金额换算
            tp_dist = entry * (fixed_tp / notional)
            sl_usd = self._dynamic_sl_usd(snap.get("symbol") or self.symbol, entry,
                                          self._sl_atr(snap.get("symbol") or self.symbol), notional)
            sl_dist = entry * (sl_usd / notional)
        else:
            atr = snap.get("atr") or 0.0
            sl_mult = float(self.ecfg.get("sl_atr_mult", 2.0))
            tp_mult = float(self.ecfg.get("tp_atr_mult", 4.0))
            if not atr:
                atr = entry * 0.01   # ATR 缺失时按 1% 兜底
            # 止损距离 = 2×ATR，但至少 min_sl_pct%（1m ATR 对小币过小，会把止损钉进噪音里秒止损）
            sl_dist = atr * sl_mult
            sl_dist = max(sl_dist, entry * float(self.ecfg.get("min_sl_pct", 1.5)) / 100)
            # 止盈距离 = 4×ATR，但至少 min_rr 倍止损距离，保证加地板后盈亏比不被破坏
            tp_dist = atr * tp_mult
            tp_dist = max(tp_dist, sl_dist * float(self.ecfg.get("min_rr", 2.0)))
        # 按开单方向设止盈止损（不做距离互换）：开多止盈在上止损在下、开空止盈在下止损在上
        sl = round(entry - sl_dist, 8) if is_long else round(entry + sl_dist, 8)
        tp = round(entry + tp_dist, 8) if is_long else round(entry - tp_dist, 8)

        # 记录最近 AI 决策（供 dashboard 展示）
        self.decision_history.append({
            "ts": time.time(), "symbol": snap.get("symbol"), "action": d.action,
            "entry": entry, "sl": sl, "tp": tp, "confidence": d.confidence,
            "risk_level": d.risk_level, "bull": d.bull, "bear": d.bear, "reason": d.reason,
        })
        self.decision_history = self.decision_history[-20:]

        # 置信度过滤：只做 confidence >= min_confidence 的单（只影响新开仓，不影响已有持仓）
        min_conf = float(self.ecfg.get("min_confidence", 70))
        if d.action in ("LONG", "SHORT") and d.confidence < min_conf:
            result["gate"] = f"CONFIDENCE_LOW({d.confidence:.0f}<{min_conf:.0f})"
            return result

        if d.action in ("LONG", "SHORT"):
            ok, reason, params = self.risk.check(d.action.lower(), entry, sl or entry, atr, risk_level=d.risk_level)
            result["risk"] = {"ok": ok, "reason": reason}
            if ok:
                sym = snap.get("symbol") or self.symbol
                self._log_decision(sym, d, entry, sl, tp, result, snap)
                # 加仓：已有同方向持仓时，不同价格都合并加仓（加权平均摊成本），不受价格高低限制
                # same_symbol_multi=true 时改为独立多仓：同币同方向也开新仓，不合并，持仓笔数不受限制
                if not self.ecfg.get("same_symbol_multi", False) and self.ecfg.get("allow_average", False):
                    pos = None
                    for p in reversed(self.paper.positions.get(sym) or []):
                        if p.side == d.action.lower():
                            pos = p
                            break
                    if pos:
                        max_avg = int(self.ecfg.get("max_averages", 3))
                        if max_avg > 0 and pos.averages >= max_avg:
                            result["risk"] = {"ok": False, "reason": f"{sym} 加仓次数已达上限 {max_avg}"}
                            return result
                        np = self.paper.average(sym, pos.side, entry, params["size_usd"],
                                                params["leverage"], sl_dist, tp_dist)
                        if np:
                            # 固定止盈止损：加仓后按新名义金额重设（金额固定，距离随名义缩放）
                            self._apply_fixed_sl_tp()
                            result["trade"] = {"side": d.action.lower(), "entry": round(np.entry, 8),
                                               "sl": np.sl, "tp": np.tp, "status": "AVERAGED"}
                            print(f"  [加仓] {sym} {d.action.lower()} 均价→{np.entry:.8g} size={np.size_usd}")
                            return result
                elif not self.ecfg.get("same_symbol_multi", False):
                    # 不加仓模式：该币已有任何持仓就不再开新仓（保证每币只开一笔，避免重复并列仓）
                    if sym in self.paper.positions:
                        result["risk"] = {"ok": False, "reason": f"{sym} 已有持仓，跳过重复开仓（allow_average=false）"}
                        return result
                # 独立多仓模式：同币同方向也开新仓，仅挡「挂单中/待确认」避免同一币重复挂单
                if sym in self.paper.pending_entries or sym in self.pending_confirm:
                    result["risk"] = {"ok": False, "reason": f"{sym} 已有挂单/待确认入场，跳过重复挂单"}
                    return result
                # 主周期出信号后，等入场周期反转确认 K 线（做多等阳线、做空等阴线）再挂单入场
                self.pending_confirm[sym] = {
                    "side": d.action.lower(), "entry_ref": entry, "sl": sl, "tp": tp,
                    "size_usd": params["size_usd"], "leverage": params["leverage"],
                    "created_at": time.time(),
                    "reverse": bool(self.ecfg.get("reverse", False)),
                    "reversed": reversed_dir,
                }
                self.risk.on_trade_opened()   # 预留仓位名额
                result["trade"] = {"side": d.action.lower(), "entry": entry, "sl": sl, "tp": tp,
                                   "status": "WAIT_CONFIRM"}
        return result

    def _rule_decision(self, snap, state, setup) -> ai.Decision:
        """确定性兜底：大模型不可用时按事件+状态给决策（仍受 Risk 约束）。"""
        atr = snap.get("atr") or 0.0
        entry = snap["price"]
        if setup == "LONG":
            return ai.Decision(action="LONG", entry=entry, sl=round(entry - 2 * atr, 8),
                               tp=round(entry + 3 * atr, 8), confidence=50.0, reason="规则兜底:突破做多")
        return ai.Decision(action="SHORT", entry=entry, sl=round(entry + 2 * atr, 8),
                           tp=round(entry - 3 * atr, 8), confidence=50.0, reason="规则兜底:跌破做空")

    # --- TG 信号 → watchlist ---
    def add_signal(self, sig):
        sym = sig.symbol
        if ":" not in sym and self.exchange is not None and getattr(self.exchange, "markets", None) is not None:
            quote = sym.partition("/")[2]
            cand = f"{sym}:{quote}"
            if cand in self.exchange.markets:
                sym = cand
        self.watchlist[sym] = {
            "side": sig.side,           # buy / sell
            "entry_ref": sig.entry_ref,
            "sl_ref": sig.stop_loss,
            "tp_ref": (sig.take_profits or [None])[0],
            "summary": sig.summary(),
        }
        self._save_watchlist()
        print(f"[watchlist] 新增监控 {sym} 方向={sig.side}")

    # --- TG 信号驱动模式：信号选交易对，实时行情决定入场 ---
    async def run_tg(self):
        import tg_source, stream
        self.exchange = self._build_exchange()   # REST：市场规范化 + OI + 回退
        self._setup_trader()   # live=true 时替换成实盘交易层
        throttle = float(self.ecfg.get("stream", {}).get("throttle", 5))
        self.streams: dict[str, object] = {}
        self.watchlist = self._load_watchlist()   # TG 模式：恢复磁盘跟单列表

        # Web Dashboard（可选）
        if self.ecfg.get("dashboard", {}).get("enabled"):
            import dashboard
            dc = self.ecfg["dashboard"]
            self.dash = dashboard.Dashboard(self, dc.get("host", "127.0.0.1"),
                                            int(dc.get("port", 8000)))
            self.dash_task = asyncio.create_task(self.dash.run())

        print(f"[engine-tg] TG 信号源启动(单币WS×N + REST回退)，决策节流 {throttle}s，事件冷却 {self.cooldown}s")

        async def ensure_stream(sym: str):
            if sym in self.streams:
                return
            try:
                st = stream.MarketStream(self.cfg["exchange"]["name"], sym, {
                    "default_type": self.cfg["exchange"].get("default_type", "swap"),
                    "aiohttp_proxy": self.cfg["exchange"].get("proxy", ""),
                    "timeframe": self.timeframe,
                })
                await st.start()
                asyncio.create_task(st.run())
                self.streams[sym] = st
                print(f"[stream] 已订阅 {sym}")
            except Exception as e:
                print(f"[stream] 订阅 {sym} 失败，走 REST 回退: {type(e).__name__}")

        async def on_signal(sig):
            self.add_signal(sig)
            for sym in list(self.watchlist.keys()):
                await ensure_stream(sym)

        self.tg_task = asyncio.create_task(tg_source.run(self.cfg, on_signal))
        while True:
            await asyncio.sleep(throttle)
            if self.dash is not None and self.dash.paused:
                continue
            for sym, info in list(self.watchlist.items()):
                # 阻塞的 REST + AI 调用丢到线程池，避免卡住 dashboard / TG 事件循环
                await asyncio.to_thread(self._process_symbol, sym, info)

    def _process_symbol(self, sym: str, info: dict):
        try:
            raw = self._stream_snapshot(sym)
            if raw is None:
                raw = self.fetch_raw(sym)   # REST 回退
            # OI 走 REST（watch_open_interest 可能不支持），带缓存避免限流
            raw["oi"] = self._fetch_oi(sym)
            raw["oi_change_5m_pct"] = self._oi_change_5m(sym, raw.get("oi"))
            if raw.get("ohlcv"):
                self.candles_cache[sym] = raw["ohlcv"][-200:]
            r = self.step(raw, signal=info)
            snap = r.get("snapshot") or {}
            self.snap_cache[sym] = snap   # 缓存快照，供 AI 定期复盘持仓用
            px = self.fetch_price(sym) or snap.get("price")
            atr = snap.get("atr") or 0.0
            self.atr_cache[sym] = atr   # 缓存 ATR 供快速结算用
            raw_ob = raw.get("orderbook") or {}
            bids = raw_ob.get("bids", [])
            asks = raw_ob.get("asks", [])
            if px:
                self.settle_at(sym, px, atr, bids=bids, asks=asks)
            # 检查限价挂单是否成交/超时
            pr = self.paper.settle_pending(sym, bids=bids, asks=asks)
            if pr == "CANCELLED":
                self.risk.on_order_cancelled()   # 撤单释放仓位名额
            # 检查 1m 反转确认是否到位（做多等阳线/做空等阴线）
            self._check_pending_confirm(sym)
            self._report(r)
        except Exception as e:
            print(f"[engine-tg] {sym} 处理出错: {e}")

    # --- 持仓结算 ---
    def _latest_candle(self, sym: str):
        """返回最近 1m K 线的 (high, low, close)，供移动止盈/止损按 K 线高低点判断（含影线）。"""
        candles = []
        st = getattr(self, "streams", None)
        if st is not None:
            if isinstance(st, dict):
                ms = st.get(sym)
                if ms is not None and getattr(ms, "healthy", False):
                    candles = getattr(ms, "klines", []) or []
            elif getattr(st, "healthy", False):
                candles = st.klines.get(sym) or []
        if not candles:
            candles = self.candles_cache.get(sym) or []
        if not candles:
            return None, None, None
        c = candles[-1]
        try:
            return float(c[2]), float(c[3]), float(c[4])
        except (IndexError, TypeError, ValueError):
            return None, None, None

    def _recent_candles(self, sym: str, n: int = 3):
        """返回最近 n 根 1m K 线 [[开,高,低,收,量], ...]，最后一根为当前未收盘的。"""
        candles = []
        st = getattr(self, "streams", None)
        if st is not None:
            if isinstance(st, dict):
                ms = st.get(sym)
                if ms is not None and getattr(ms, "healthy", False):
                    candles = getattr(ms, "klines", []) or []
            elif getattr(st, "healthy", False):
                candles = st.klines.get(sym) or []
        if not candles:
            candles = self.candles_cache.get(sym) or []
        if not candles:
            return []
        out = []
        for c in candles[-n:]:
            try:
                out.append([float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
            except (IndexError, TypeError, ValueError):
                continue
        return out

    def _kline_reversal(self, sym: str, side: str) -> bool:
        """开仓后实时监控 K 线：出现「放量反转 K 线」时返回 True，用于及时止盈。"""
        candles = self._recent_candles(sym, 3)
        if len(candles) < 2:
            return False
        prev = candles[-2]   # 上一根已收盘
        cur = candles[-1]    # 当前根（实时）
        po, ph, pl, pc, pv = prev
        co, ch, cl, cc, cv = cur
        vol_up = bool(pv > 0 and cv >= pv * 1.3)
        if side == "long":
            # 做多反转：当前根收阴 + 跌破上一根低点 + 放量
            return bool(cc < co and cc < pl and vol_up)
        # 做空反转：当前根收阳 + 突破上一根高点 + 放量
        return bool(cc > co and cc > ph and vol_up)

    def _choch_exit(self, sym: str, side: str) -> bool:
        """用高周期 K 线趋势方向判断是否提前止盈。

        4h 趋势 = EMA20 vs EMA50 均线排列（直观的趋势定义，非 swing points 结构）：
          多头趋势：EMA20 > EMA50
          空头趋势：EMA20 < EMA50

        做多持仓 → 4h 趋势转空（EMA20 下穿 EMA50）→ 提前止盈
        做空持仓 → 4h 趋势转多（EMA20 上穿 EMA50）→ 提前止盈

        只用已收盘 K 线（去掉最后一根未收盘的），避免信号随实时价抖动。
        """
        tf = self.ecfg.get("choch_timeframe", "4h")
        candles = self.fetch_candles(sym, tf, limit=120)
        if not candles or len(candles) < 55:
            return False
        # 去掉最后一根（可能是未收盘的），只用已收盘 K 线算趋势
        closed = candles[:-1]
        if len(closed) < 55:
            return False
        closes = [c[4] for c in closed]
        ema20 = features.ema(closes, 20)
        ema50 = features.ema(closes, 50)
        e20 = ema20[-1]
        e50 = ema50[-1]
        if side == "long":
            # 做多：4h 趋势转空（EMA20 已下穿 EMA50）
            return e20 < e50
        # 做空：4h 趋势转多（EMA20 已上穿 EMA50）
        return e20 > e50

    def settle_at(self, sym: str, px: float, atr: float, bids=None, asks=None):
        self.last_prices[sym] = px   # 记录最新价供 dashboard 计算浮盈
        plist = self.paper.positions.get(sym) or []
        if not plist:
            return None
        entry = abs(plist[0].entry) if plist else abs(px)
        # 动态止盈止损距离：
        # 固定模式下用固定金额换算；否则用 ATR。保本/移动止损/K线反转动态逻辑始终启用。
        fixed_tp = float(self.ecfg.get("fixed_tp_usd", 0.0) or 0.0)
        fixed_sl = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
        if fixed_tp > 0 and fixed_sl > 0:
            # 固定金额换算价格距离（按币种总名义金额）；止损金额走 ATR 动态自适应
            total_notional = sum(p.size_usd * p.leverage for p in plist)
            if total_notional > 0:
                sl_usd = self._dynamic_sl_usd(sym, entry, self._sl_atr(sym), total_notional)
                sl_dist = entry * (sl_usd / total_notional)
                tp_dist = entry * (fixed_tp / total_notional)
                # 分级移动止损：盈利越多回撤容忍越小（trailing_stop_positive 语义）
                # 基础档：浮盈达标后回撤容忍 = trail_step1_usd（默认 2u）
                # 收紧档：浮盈超过 trail_step2_after_usd（默认 4u）后，回撤容忍收紧到 trail_step2_usd（默认 1u）
                trail = entry * (float(self.ecfg.get("trail_step1_usd", 2.0) or 0.0) / total_notional)
                trail_tight = entry * (float(self.ecfg.get("trail_step2_usd", 1.0) or 0.0) / total_notional)
                tighten = entry * (float(self.ecfg.get("trail_step2_after_usd", 4.0) or 0.0) / total_notional)
                # 保本门槛：浮盈超过此值后止损移到成本价（默认 0.5×止损距离）
                be = sl_dist * float(self.ecfg.get("break_even_ratio", 0.5))
            else:
                sl_dist = tp_dist = be = trail = trail_tight = tighten = 0.0
        else:
            trail_mult = float(self.trailing_cfg.get("trail_atr_mult", 1.5))
            be_mult = float(self.trailing_cfg.get("break_even_at", 0.5))
            be_pct = float(self.trailing_cfg.get("profit_lock_pct", 0.0)) / 100
            trail_pct = float(self.trailing_cfg.get("trail_pct", 0.0)) / 100
            min_pct = float(self.trailing_cfg.get("min_stop_pct", 0.5)) / 100
            floor = entry * min_pct
            trail = max(atr * trail_mult, floor) if atr else floor
            be = max(atr * be_mult, floor * 0.5) if atr else floor * 0.5
            if be_pct > 0:
                be = min(be, entry * be_pct)
            if trail_pct > 0:
                trail = min(trail, entry * trail_pct)
            # 非固定模式：分级移动止损的收紧档不启用，置 0 避免下游引用未定义
            trail_tight = 0.0
            tighten = 0.0
        # 固定止盈模式：触及 TP/SL 立即平仓；动态止盈模式：锁利 + 分级移动止损继续奔跑
        dynamic_tp = bool(self.ecfg.get("dynamic_tp", False))
        # 固定止盈止损模式：服务器端条件单为主（开仓后立即挂 reduce-only 单，毫秒级触发），
        # 本地 mark 平仓保留作为兜底——处理「价格已越过 TP/SL 导致服务器端 -2021 挂单失败」的裸奔单。
        # 正常情况服务器端先触发，本地 mark 随后平仓时因仓位已无而 ReduceOnly 被拒（-2022），
        # 该报错在 live._exit_fill 里已被忽略并返回真实价，不会重复记账。
        # 时间衰减止盈 + 时间衰减止损：持仓越久，止盈门槛越低、止损越紧（释放名额 + 死扛单尽早离场）
        if (not dynamic_tp) and self.ecfg.get("roi_decay_enabled", False):
            self._apply_roi_decay(plist, fixed_tp)
        if self.ecfg.get("sl_decay_enabled", False):
            self._apply_sl_decay(plist)
        _sl_before = {id(p): p.sl for p in plist}
        closed = self.paper.mark(sym, px, bids=bids, asks=asks,
                                 trail_dist=trail, break_even_dist=be,
                                 trail_tight_dist=trail_tight if dynamic_tp else None,
                                 tighten_dist=tighten if dynamic_tp else None,
                                 fixed_mode=not dynamic_tp)
        # 动态止盈锁利后，把「上移/下移的止损」同步到币安服务器端（引擎断线也能按锁利位止损）
        if dynamic_tp and self.ecfg.get("live", False) and hasattr(self.paper, "_sync_server_sl"):
            for p in plist:
                if p.sl is not None and p.sl != _sl_before.get(id(p)):
                    try:
                        self.paper._sync_server_sl(sym, p.side, p.sl)
                    except Exception as e:
                        print(f"[dynamic_tp] 同步 {sym} 止损失败: {e}")
        if closed:
            for c in closed:
                self.risk.on_trade_closed(c.pnl)
                self._log_outcome(sym, c)
                print(f"  [平仓] {c.side} {c.symbol} {c.exit_reason} pnl={c.pnl}")
                # 止损反手：固定止损触发后，反向开一单（多→空、空→多，顺势）
                if self.ecfg.get("stop_reverse_enabled", False) and c.exit_reason == "STOP_LOSS":
                    self._reverse_open_after_stop(sym, c.side, px, bids, asks)
            return closed[-1]
        # 及时止盈：浮盈达到门槛后，出现放量反转 K 线才止盈（避免微利就被扫，让利润先跑）
        if self.trailing_cfg.get("kline_reversal", True):
            min_rev_pct = float(self.trailing_cfg.get("kline_reversal_min_pnl_pct", 1.0)) / 100
            for p in plist:
                pnl_pct = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
                if pnl_pct >= min_rev_pct and self._kline_reversal(sym, p.side):
                    cl = self.paper.close_position(sym, px, bids=bids, asks=asks, reason="KLINE_REVERSAL")
                    for c in cl:
                        self.risk.on_trade_closed(c.pnl)
                        self._log_outcome(sym, c)
                        print(f"  [平仓] {c.side} {c.symbol} {c.exit_reason} pnl={c.pnl}")
                    if cl:
                        return cl[-1]
        # CHoCH 结构反转提前止盈：浮盈时高周期结构转变（1h/4h）→ 在趋势反转前离场，利润最大化 + 缩短持仓时间
        if self.ecfg.get("choch_exit_enabled", False):
            min_pnl_usd = float(self.ecfg.get("choch_min_pnl_usd", 0.5))
            for p in plist:
                pnl_usd = p.unrealized_pnl(px)
                if pnl_usd >= min_pnl_usd and self._choch_exit(sym, p.side):
                    cl = self.paper.close_position(sym, px, bids=bids, asks=asks, reason="CHoCH_REVERSAL")
                    for c in cl:
                        self.risk.on_trade_closed(c.pnl)
                        self._log_outcome(sym, c)
                        print(f"  [平仓] {c.side} {c.symbol} {c.exit_reason} pnl={c.pnl}")
                    if cl:
                        return cl[-1]
        # CHoCH 趋势反转提前止损：已关闭。
        # 实证发现：浮亏时 4h EMA 在震荡市频繁交叉，导致反复小亏 + 反复开单的恶性循环
        # （CHoCH_STOP 68笔胜率0% -7.85u）。浮亏单改由固定止损 2u 兜底，不再用 4h 反转提前止损。
        return closed[-1] if closed else None

    # --- 决策日志（决策 + 结果，供复盘分析） ---
    def _append_log(self, entry: dict):
        try:
            with open(self.decision_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[engine] 决策日志写入失败: {e}")

    def _log_decision(self, sym, d, entry, sl, tp, result, snap=None):
        """记录决策 + 开仓时的量价/订单流特征，供后续「盈利特征寻优」分析。"""
        rec = {
            "type": "decision", "ts": time.time(), "symbol": sym,
            "action": d.action, "entry": entry, "sl": sl, "tp": tp,
            "confidence": d.confidence, "risk_level": d.risk_level,
            "bull": d.bull, "bear": d.bear, "reason": d.reason,
            "events": result.get("events"), "gate": result.get("gate"),
        }
        # 记录量价/订单流特征（snap 里已有的数值），供后续寻优「哪些特征能区分盈利单」
        if snap:
            flow = snap.get("orderflow") or {}
            vol = snap.get("volume") or {}
            rec["feat"] = {
                "taker_buy_ratio": flow.get("taker_buy_ratio"),
                "cvd_trend": flow.get("cvd_trend"),
                "cvd_direction": flow.get("cvd_direction"),
                "delta_5m": flow.get("delta_5m"),
                "delta_1m": flow.get("delta_1m"),
                "large_trades": flow.get("large_trades"),
                "vol_ratio": snap.get("vol_ratio"),
                "vol_trend": vol.get("trend"),
                "vol_spike": bool(vol.get("spike")),
                "rsi14": snap.get("rsi14"),
                "atr_pct": snap.get("atr_pct"),
                "macd_hist": (snap.get("macd") or {}).get("hist"),
                "ob_imbalance": (snap.get("orderbook") or {}).get("imbalance"),
                "trend_short": (snap.get("trend") or {}).get("short"),
                "trend_mid": (snap.get("trend") or {}).get("mid"),
            }
        self._append_log(rec)

    def _log_outcome(self, sym, trade):
        self._append_log({
            "type": "outcome", "ts": time.time(), "symbol": sym,
            "side": trade.side, "entry": trade.entry, "exit": trade.exit,
            "pnl": trade.pnl, "reason": trade.exit_reason,
        })

    def settle(self):
        for sym in list(self.paper.positions.keys()):
            try:
                self._rest_wait()
                t = self.exchange.fetch_ticker(sym)
                px = t["last"]
                self._rest_wait()
                raw = features.build_snapshot({"symbol": sym, "ohlcv": self.exchange.fetch_ohlcv(sym, self.timeframe, limit=30)})
                atr = raw.get("atr") or 0.0
                self.settle_at(sym, px, atr)
            except Exception as e:
                print(f"  settle {sym} 失败: {e}")

    def _settle_symbol(self, sym: str):
        """只按最新价结算持仓，不触发决策/开仓（用于已下榜但仍持仓的币）。"""
        # 交易所没有该币（如切换交易所后遗留的旧仓位），跳过
        if self.exchange is not None and getattr(self.exchange, "markets", None) is not None and sym not in self.exchange.markets:
            return
        try:
            raw = self.fetch_raw(sym)
            snap = features.build_snapshot(raw)
            px = snap.get("price")
            atr = snap.get("atr") or 0.0
            raw_ob = raw.get("orderbook") or {}
            if px:
                self.settle_at(sym, px, atr, bids=raw_ob.get("bids", []), asks=raw_ob.get("asks", []))
        except Exception as e:
            print(f"[engine] {sym} 结算出错: {e}")

    # --- AI 定期复盘持仓：收紧止损 / 上调止盈 / 继续持有 ---
    def _ai_review_position(self, sym: str):
        """对该币的每一笔持仓分别做一次 AI 复盘（在 worker 线程里跑，阻塞的 Ollama 调用不影响事件循环）。"""
        plist = self.paper.positions.get(sym)
        if not plist:
            return
        # 复盘用与决策完全同源的最新快照：用 WS 实时行情重建，而非缓存的旧 snap_cache
        raw = self._stream_snapshot(sym)
        if raw is None:
            raw = self.fetch_raw(sym)   # REST 回退
        raw["oi"] = self._fetch_oi(sym)
        raw["oi_change_5m_pct"] = self._oi_change_5m(sym, raw.get("oi"))
        snap = features.build_snapshot(raw)
        if not snap:
            return
        atr = snap.get("atr") or 0.0
        trail_pct = float(self.trailing_cfg.get("trail_pct", 0.0)) / 100
        for pos in list(plist):
            px = self.fetch_price(sym) or snap.get("price") or pos.entry
            pos_info = {
                "side": pos.side, "entry": round(pos.entry, 8), "size_usd": pos.size_usd,
                "sl": pos.sl, "tp": pos.tp,
                "price": round(px, 8),
                "pnl": round(pos.unrealized_pnl(px), 2),
            }
            r = ai.review_position(self.local_cfg, pos_info, snap)
            if not r:
                continue
            action = r["action"]
            # 复盘期间价格可能已变化，用最新价重新落地，避免拿旧价收紧
            px = self.fetch_price(sym) or px
            # 持仓若在复盘期间已被平仓，跳过这一笔
            if pos not in self.paper.positions.get(sym, []):
                continue
            tighten = atr if atr > 0 else pos.entry * trail_pct
            if trail_pct > 0:
                tighten = min(tighten, pos.entry * trail_pct)
            if action == "tighten_sl":
                if pos.side == "long" and pos.sl is not None and px > pos.entry and tighten > 0:
                    pos.sl = max(pos.sl, round(px - tighten, 8))
                    print(f"[AI复盘] {sym} 收紧止损 → {pos.sl}（{r['reason']}）")
                elif pos.side == "short" and pos.sl is not None and px < pos.entry and tighten > 0:
                    pos.sl = min(pos.sl, round(px + tighten, 8))
                    print(f"[AI复盘] {sym} 收紧止损 → {pos.sl}（{r['reason']}）")
                else:
                    print(f"[AI复盘] {sym} 建议收紧止损但未满足条件，保持（{r['reason']}）")
            elif action == "cut_loss":
                # 浮亏且趋势转坏：把止损向当前价收紧（做多抬高、做空下移），提前离场控制亏损
                if pos.side == "long" and pos.sl is not None and px < pos.entry and tighten > 0:
                    new_sl = round(px - tighten, 8)
                    if new_sl > pos.sl:
                        pos.sl = new_sl
                        print(f"[AI复盘] {sym} 趋势转坏提前止损 → {pos.sl}（{r['reason']}）")
                    else:
                        print(f"[AI复盘] {sym} 建议提前止损但当前止损已更紧，保持（{r['reason']}）")
                elif pos.side == "short" and pos.sl is not None and px > pos.entry and tighten > 0:
                    new_sl = round(px + tighten, 8)
                    if new_sl < pos.sl:
                        pos.sl = new_sl
                        print(f"[AI复盘] {sym} 趋势转坏提前止损 → {pos.sl}（{r['reason']}）")
                    else:
                        print(f"[AI复盘] {sym} 建议提前止损但当前止损已更紧，保持（{r['reason']}）")
                else:
                    print(f"[AI复盘] {sym} 未满足提前止损条件，保持（{r['reason']}）")
            elif action == "raise_tp":
                if pos.side == "long" and px > pos.entry and atr > 0:
                    pos.tp = round(px + 2 * atr, 8)
                    print(f"[AI复盘] {sym} 上调止盈 → {pos.tp}（{r['reason']}）")
                elif pos.side == "short" and px < pos.entry and atr > 0:
                    pos.tp = round(px - 2 * atr, 8)
                    print(f"[AI复盘] {sym} 上调止盈 → {pos.tp}（{r['reason']}）")
                else:
                    print(f"[AI复盘] {sym} 建议上调止盈但未满足条件，保持（{r['reason']}）")
            else:
                # 兜底：模型输出 hold 但 reason 里明确要收紧/保护利润 → 按收紧处理
                if ("收紧" in r["reason"] or "保护" in r["reason"]) and pos.unrealized_pnl(px) > 0:
                    if pos.side == "long" and pos.sl is not None and px > pos.entry and tighten > 0:
                        pos.sl = max(pos.sl, round(px - tighten, 8))
                        print(f"[AI复盘] {sym} 兜底收紧止损 → {pos.sl}（{r['reason']}）")
                    elif pos.side == "short" and pos.sl is not None and px < pos.entry and tighten > 0:
                        pos.sl = min(pos.sl, round(px + tighten, 8))
                        print(f"[AI复盘] {sym} 兜底收紧止损 → {pos.sl}（{r['reason']}）")
                    else:
                        print(f"[AI复盘] {sym} 继续持有（{r['reason']}）")
                else:
                    print(f"[AI复盘] {sym} 继续持有（{r['reason']}）")
        self.paper._save()

    async def _ai_review_loop(self):
        """独立 AI 复盘循环：与快速结算并行，复盘期间止盈止损照常高频检查。"""
        interval = float(self.ecfg.get("ai_review_interval_min", 5)) * 60
        tick = min(30.0, interval)
        while True:
            await asyncio.sleep(tick)
            try:
                await self._ai_review_round()
            except Exception as e:
                print(f"[ai-review] 循环出错: {e}")

    async def _ai_review_round(self):
        """每轮复盘所有到期的持仓（走线程串行），每个币按自己的时间戳独立计时，避免饿死后面的持仓。"""
        interval = float(self.ecfg.get("ai_review_interval_min", 5)) * 60
        now = time.time()
        due = []
        for sym in list(self.paper.positions.keys()):
            if sym not in self.last_ai_review:
                self.last_ai_review[sym] = now   # 首次标记，本轮跳过
                continue
            if now - self.last_ai_review[sym] >= interval:
                due.append(sym)
        for sym in due:
            self.last_ai_review[sym] = now
            await asyncio.to_thread(self._ai_review_position, sym)

    # --- 快速结算循环：用 WS 实时价高频检查止盈止损（与慢决策轮询并行） ---
    async def fast_settle(self):
        interval = float(self.ecfg.get("stream", {}).get("settle_interval", 2))
        last_reconcile = 0.0
        total_profit_target = float(self.ecfg.get("close_all_profit_usd", 0))   # 总浮盈达此金额全平，0=禁用
        while True:
            await asyncio.sleep(interval)
            try:
                # 总浮盈达目标即全平：监控全部持仓浮盈，达到 close_all_profit_usd 就平掉所有持仓
                if total_profit_target > 0:
                    total_upnl = 0.0
                    for sym in list(self.paper.positions.keys()):
                        px = self._stream_price(sym) or self.last_prices.get(sym)
                        if px is None:
                            continue
                        for p in self.paper.positions.get(sym, []):
                            total_upnl += p.unrealized_pnl(px)
                    if total_upnl >= total_profit_target:
                        n = await asyncio.to_thread(self.close_all_positions, "PROFIT_CLOSE_ALL")
                        if n:
                            print(f"[PROFIT_CLOSE_ALL] 总浮盈 {total_upnl:.2f}u 达标，已平掉 {n} 个持仓")
                for sym in list(self.paper.positions.keys()):
                    px = self._stream_price(sym)
                    bids, asks = self._stream_book(sym)
                    if px is None:
                        px = self.last_prices.get(sym)
                    if px is None:
                        continue
                    atr = self.atr_cache.get(sym, 0.0)
                    self.settle_at(sym, px, atr, bids=bids, asks=asks)
                # 待 1m 确认的入场单也要轮询（即使该币已下榜）
                for sym in list(self.pending_confirm.keys()):
                    self._check_pending_confirm(sym)
                # 挂单(限价)也要轮询成交/超时（即使该币已下榜），超时自动撤单
                # 传入 WS 盘口，支持「追踪建仓」追挂更优价
                for sym in list(self.paper.pending_entries.keys()):
                    b, a = self._stream_book(sym)
                    pr = await asyncio.to_thread(self.paper.settle_pending, sym, bids=b, asks=a)
                    if pr == "CANCELLED":
                        self.risk.on_order_cancelled()
                # 持仓对账：每 15 秒查一次币安实际持仓，同步服务器端止盈止损单触发的平仓
                now = time.time()
                if now - last_reconcile >= 15 and self.ecfg.get("live", False):
                    last_reconcile = now
                    await asyncio.to_thread(self._reconcile_positions)
            except Exception as e:
                print(f"[fast-settle] 出错: {e}")

    def _reconcile_positions(self):
        """持仓对账：检查本地持仓在币安的实际数量，若已被服务器端止盈止损单平掉，本地同步记录平仓。

        平仓价从币安最近成交(fetch_my_trades)取最后一笔卖出/买入成交价，盈亏精确。
        同时用币安真实余额校准本地 equity，保证风控基于真实资金。
        """
        if not self.ecfg.get("live", False):
            return
        try:
            # 用币安真实余额校准 equity（本地 pnl 记账有累计误差，定期拉真实值纠正）
            try:
                self._rest_wait()
                bal = self.exchange.fetch_balance()
                usdt = bal.get("USDT") or {}
                total = float(usdt.get("total") or 0.0)
                if total > 0:
                    self.paper.equity = total
                    self.risk.state.equity = total
                    # peak 校准：若本地 peak 明显高于真实余额（历史虚高记账污染），重置 peak 为真实余额，
                    # 避免「回撤」被假峰值错误放大导致误熔断。正常情况 peak 应 >= equity，取 max。
                    cur_peak = self.risk.state.peak_equity
                    if cur_peak > total * 1.2:   # peak 比真实余额高 20% 以上，判定为历史虚高，重置
                        self.risk.state.peak_equity = total
                        self.paper.peak_equity = total
                    else:
                        self.risk.state.peak_equity = max(cur_peak, total)
            except Exception as e:
                self._note_rest_ban(str(e))
                if self._rest_banned():
                    return   # 封禁期内跳过对账，避免继续打 REST
                pass
            try:
                self._rest_wait()
                pos_list = self.exchange.fetch_positions()
            except Exception as e:
                self._note_rest_ban(str(e))
                return
            # 币安实际持仓（contracts != 0）
            actual = {p["symbol"]: p for p in pos_list if abs(float(p.get("contracts") or 0)) > 0}
            local_syms = set(self.paper.positions.keys())
            binance_syms = set(actual.keys())
            # 1) 本地有、币安无 → 已被服务器端条件单平仓，本地同步平仓
            for sym in local_syms - binance_syms:
                if hasattr(self.paper, "_cancel_tp_sl"):
                    try:
                        self.paper._cancel_tp_sl(sym)
                    except Exception:
                        pass
                local = self.paper.positions.get(sym)
                if not local:
                    continue
                for pos in list(local):
                    close_side = "sell" if pos.side == "long" else "buy"
                    exit_px = self._last_fill_price(sym, close_side)
                    if exit_px is None:
                        exit_px = self._stream_price(sym) or self.last_prices.get(sym)
                    if exit_px is None:
                        continue
                    t = self.paper._close(sym, pos, exit_px, "TP_SL_SERVER")
                    if t:
                        self.risk.on_trade_closed(t.pnl)
                        self._log_outcome(sym, t)
                        print(f"  [对账平仓] {t.side} {t.symbol} {t.exit_reason} pnl={t.pnl} @ {exit_px:.8g}")
                        # 止损反手：服务器端止损单触发（pnl<0）→ 反向开一单（多→空、空→多，顺势）
                        if self.ecfg.get("stop_reverse_enabled", False) and t.pnl < 0:
                            px = self._stream_price(sym) or self.last_prices.get(sym) or exit_px
                            if px:
                                self._reverse_open_after_stop(sym, t.side, px)
            # 2) 币安有、本地无 → 本地状态缺失（手动开仓或状态丢失），拉回本地记账
            added_any = False
            for sym in binance_syms - local_syms:
                ap = actual[sym]
                try:
                    side = "long" if (ap.get("side") or "").lower() == "long" else "short"
                    entry = float(ap.get("entryPrice") or 0)
                    contracts = abs(float(ap.get("contracts") or 0))
                    if entry <= 0 or contracts <= 0:
                        continue
                    # 反推保证金 size_usd = 名义 / 杠杆，用默认杠杆 20
                    lev = int(self.ecfg.get("risk", {}).get("leverage", 20))
                    size_usd = round(contracts * entry / lev, 2)
                    pos = papermod.Position(symbol=sym, side=side, entry=entry, size_usd=size_usd,
                                            leverage=lev, sl=None, tp=None, highest=entry, lowest=entry)
                    self.paper.positions.setdefault(sym, []).append(pos)
                    print(f"  [对账补录] 币安多出持仓 {sym} {side} entry={entry} size={size_usd}")
                    added_any = True
                except Exception as e:
                    print(f"  [对账补录] {sym} 失败: {e}")
            # 补录完成后只重设一次止盈止损（避免循环里全量重挂刷屏）
            if added_any:
                self._apply_fixed_sl_tp()
            # 同步风控持仓数
            self.risk.state.open_positions = sum(len(ps) for ps in self.paper.positions.values())
        except Exception as e:
            print(f"[engine] 持仓对账出错: {e}")

    def _last_fill_price(self, sym: str, side: str | None = None):
        """取该币最近一笔「指定方向」成交的成交价（用于对账平仓时确定真实平仓价）。

        side='sell' → 找最近的卖出成交（平多）；side='buy' → 找最近的买入成交（平空）。
        只取 reduceOnly 平仓方向的成交，避免把开仓/加仓成交误当平仓价。
        """
        try:
            self._rest_wait()
            trades = self.exchange.fetch_my_trades(sym, limit=50)
            if not trades:
                return None
            # 优先找 reduceOnly 且方向匹配的成交；找不到再退化为方向匹配
            for t in reversed(trades):
                t_side = (t.get("side") or "").lower()
                reduce_only = bool(t.get("reduceOnly")) or bool((t.get("info") or {}).get("reduceOnly"))
                if side and t_side == side and reduce_only:
                    return float(t.get("price") or 0) or None
            for t in reversed(trades):
                t_side = (t.get("side") or "").lower()
                if side and t_side == side:
                    return float(t.get("price") or 0) or None
        except Exception as e:
            self._note_rest_ban(str(e))
            pass
        return None

    # --- 手动平仓（dashboard 按钮触发） ---
    def manual_close(self, sym: str):
        try:
            px = self._stream_price(sym)
            bids, asks = self._stream_book(sym)
            if px is None:
                px = self.fetch_price(sym)
            if px is None:
                return None
            cl = self.paper.close_position(sym, px, bids=bids, asks=asks)
            for c in cl:
                self.risk.on_trade_closed(c.pnl)
                self._log_outcome(sym, c)
                print(f"  [手动平仓] {c.side} {c.symbol} pnl={c.pnl}")
            return cl[-1] if cl else None
        except Exception as e:
            print(f"[engine] 手动平仓 {sym} 出错: {e}")
            return None

    def close_all_positions(self, reason: str = "CLOSE_ALL") -> int:
        """平掉所有持仓（定时全平用），返回平掉的币数。"""
        n = 0
        for sym in list(self.paper.positions.keys()):
            try:
                px = self._stream_price(sym) or self.fetch_price(sym) or self.last_prices.get(sym)
                bids, asks = self._stream_book(sym)
                if px is None:
                    continue
                cl = self.paper.close_position(sym, px, bids=bids, asks=asks, reason=reason)
                for c in cl:
                    self.risk.on_trade_closed(c.pnl)
                    self._log_outcome(sym, c)
                    print(f"  [{reason}] {c.side} {c.symbol} pnl={c.pnl}")
                n += 1
            except Exception as e:
                print(f"[{reason}] 平仓 {sym} 出错: {e}")
        return n

    def _reverse_open_after_stop(self, sym: str, closed_side: str, px: float, bids=None, asks=None):
        """止损反手：固定止损触发后，反向开一单（多→空、空→多，顺势）。

        按开单方向设固定止盈 2u / 止损 0.5u，直接实盘开仓并挂服务器端条件单。
        """
        new_side = "short" if closed_side == "long" else "long"
        size_usd = float(self.ecfg.get("risk", {}).get("max_position_usd", 5.0))
        leverage = int(self.ecfg.get("risk", {}).get("leverage", 20))
        notional = size_usd * leverage
        fixed_tp = float(self.ecfg.get("fixed_tp_usd", 0.0) or 0.0)
        fixed_sl = float(self.ecfg.get("fixed_sl_usd", 0.0) or 0.0)
        if notional <= 0 or fixed_tp <= 0 or fixed_sl <= 0:
            return
        tp_dist = px * (fixed_tp / notional)
        sl_dist = px * (fixed_sl / notional)
        if new_side == "long":
            sl = round(px - sl_dist, 8)
            tp = round(px + tp_dist, 8)
        else:
            sl = round(px + sl_dist, 8)
            tp = round(px - tp_dist, 8)
        try:
            res = self.paper.open(sym, new_side, px, size_usd, leverage, sl, tp,
                                  bids=bids, asks=asks, reversed=False)
            if res and res != "PENDING":
                self.risk.on_trade_opened()
                print(f"  [止损反手] {sym} {closed_side}止损 → 开{new_side} @ {px:.8g} sl={sl} tp={tp}")
        except Exception as e:
            print(f"  [止损反手] {sym} 反向开仓失败: {e}")

    # --- 涨幅榜信号源模式：监控 24h 涨幅 Top N ---
    async def run_gainers(self):
        import gainers, momentum, stream
        self.exchange = self._build_exchange()
        self._setup_trader()   # live=true 时替换成实盘交易层
        throttle = float(self.ecfg.get("stream", {}).get("throttle", 5))
        # 合并连接：一个 MultiStream 订阅所有交易对（~3 条 WS，而非 N×4）
        self.streams = stream.MultiStream(self.cfg["exchange"]["name"], {
            "default_type": self.cfg["exchange"].get("default_type", "swap"),
            "aiohttp_proxy": self.cfg["exchange"].get("proxy", ""),
            "timeframe": self.timeframe,
        })
        self.stream_task = asyncio.create_task(self.streams.run())
        gcfg = self.ecfg.get("gainers") or {}
        top_n = int(gcfg.get("top_n", 10))
        refresh_min = float(gcfg.get("refresh_min", 5))
        min_qv = float(gcfg.get("min_quote_volume", 0))
        max_gain = float(gcfg.get("max_gain_pct", 0))   # >0 时过滤涨幅过高的妖币
        pool_size = int(gcfg.get("pool_size", 80))      # 动量筛选：只对最活跃的 N 个拉K线
        pool_type = str(gcfg.get("signal_pool", "momentum")).lower()  # momentum=多周期平滑动量 | gainers=24h涨幅榜
        # 方向：long=回踩做多 / short=跌破做空 / both=多空都做
        direction = str(gcfg.get("direction", "long")).lower()
        if direction == "both":
            side = "both"
            trigger = "both"
            dir_label = "多空都做(回踩多/跌破空)"
        elif direction == "short":
            side = "sell"
            trigger = "breakdown"
            dir_label = "做空(跌破)"
        else:
            side = "buy"
            trigger = "pullback"
            dir_label = "做多(回踩)"
        self.ecfg["entry_trigger"] = trigger   # 覆盖入场触发

        # Web Dashboard（可选）
        if self.ecfg.get("dashboard", {}).get("enabled"):
            import dashboard
            dc = self.ecfg["dashboard"]
            self.dash = dashboard.Dashboard(self, dc.get("host", "127.0.0.1"),
                                            int(dc.get("port", 8000)))
            self.dash_task = asyncio.create_task(self.dash.run())

        async def refresh_gainers():
            try:
                if pool_type == "momentum":
                    top = await asyncio.to_thread(momentum.fetch_top_momentum, self.exchange, top_n, min_qv, pool_size)
                else:
                    top = await asyncio.to_thread(gainers.fetch_top_gainers, self.exchange, top_n, min_qv, max_gain)
                if not top:
                    print("[gainers] 未抓到榜单，稍后重试")
                    return
                self.gainer_board = top   # 完整榜单，供 dashboard 展示涨跌幅/动量分/各周期收益
                new_wl = {}
                for g in top:
                    new_wl[g["symbol"]] = {
                        "side": side,              # 跟单方向：做空/做多
                        "entry_ref": None,
                        "sl_ref": None,
                        "tp_ref": None,
                        "summary": f"[{dir_label}] {g['change_pct']:+.2f}%",
                    }
                dropped = [s for s in self.watchlist if s not in new_wl]
                self.watchlist = new_wl
                names = ", ".join(f"{g['symbol'].split('/')[0]}({g['change_pct']:+.1f}%)" for g in top)
                print(f"[gainers] 刷新 Top{len(new_wl)}: {names}")
                if dropped:
                    print(f"[gainers] 下榜(只结算不再开新仓): {', '.join(s.split('/')[0] for s in dropped)}")
                    for s in dropped:
                        self.paper.cancel_pending(s)   # 下榜币撤掉未成交挂单
                # 合并订阅当前榜单交易对（集合变化时 MultiStream 自动重连重订阅）
                self.streams.set_symbols(list(new_wl.keys()))
            except Exception as e:
                print(f"[gainers] 刷新失败: {e}")

        print(f"[engine-gainers] 动量信号源启动：Top{top_n}，{dir_label}，入场触发={trigger}，每 {refresh_min} 分钟刷新，决策节流 {throttle}s，事件冷却 {self.cooldown}s（合并WS连接）")
        self.fast_settle_task = asyncio.create_task(self.fast_settle())   # 快速结算循环（并行）
        if self.ecfg.get("ai_review", False):
            self.ai_review_task = asyncio.create_task(self._ai_review_loop())   # AI 复盘循环（并行，不阻塞结算）
        await refresh_gainers()
        next_refresh = time.time() + refresh_min * 60
        while True:
            await asyncio.sleep(throttle)
            if time.time() >= next_refresh:
                await refresh_gainers()
                next_refresh = time.time() + refresh_min * 60
            if self.dash is not None and self.dash.paused:
                continue
            for sym, info in list(self.watchlist.items()):
                await asyncio.to_thread(self._process_symbol, sym, info)
            # 已下榜但仍持仓的币：只结算，不再开新仓
            for sym in list(self.paper.positions.keys()):
                if sym not in self.watchlist:
                    await asyncio.to_thread(self._settle_symbol, sym)
            # 记录权益曲线（每轮一次，最多保留 500 点）
            self.equity_history.append([time.time(), self.paper.equity])
            self.equity_history = self.equity_history[-500:]

    # --- WebSocket 实时模式 ---
    async def run_ws(self):
        import stream
        scfg = self.ecfg.get("stream") or {}
        throttle = float(scfg.get("throttle", 5))
        st = stream.MarketStream(self.cfg["exchange"]["name"], self.symbol, {
            "default_type": self.cfg["exchange"].get("default_type", "swap"),
            "proxy": scfg.get("proxy") or self.cfg["exchange"].get("proxy", ""),
            "timeframe": self.timeframe,
        })
        print(f"[engine-ws] 订阅 {self.symbol} (Trade/OrderBook/Kline)，决策节流 {throttle}s，事件冷却 {self.cooldown}s")
        await st.start()
        asyncio.create_task(st.run())
        while True:
            await asyncio.sleep(throttle)
            try:
                raw = st.snapshot()
                r = self.step(raw)
                snap = r.get("snapshot") or features.build_snapshot(raw)
                px = st.last_price or snap.get("price")
                atr = snap.get("atr") or 0.0
                if px:
                    for sym in list(self.paper.positions.keys()):
                        if sym == self.symbol:
                            self.settle_at(sym, px, atr)
                self._report(r)
            except Exception as e:
                print(f"[engine-ws] 循环出错: {e}")

    # --- 主循环（REST 轮询）---
    def run(self):
        print(f"[engine] 启动，symbol={self.symbol}，轮询 {self.poll}s，事件冷却 {self.cooldown}s")
        self.exchange = self._build_exchange()
        self._setup_trader()   # live=true 时替换成实盘交易层
        while True:
            try:
                raw = self.fetch_raw()
                r = self.step(raw)
                self.settle()
                self._report(r)
            except Exception as e:
                print(f"[engine] 循环出错: {e}")
            time.sleep(self.poll)

    def _report(self, r):
        snap = r.get("snapshot") or {}
        if not r.get("events"):
            return
        sym = snap.get("symbol") or "?"
        print(f"[事件] {sym} {r['events']} @ {snap.get('price')}")
        if r.get("gate"):
            print(f"[机会门] {sym} {r['gate']}")
        if r.get("decision"):
            print(f"[决策] {sym} {r['decision']}")
        if r.get("risk"):
            print(f"[风控] {sym} {r['risk']}")
        if r.get("trade"):
            label = "实盘开仓" if self.ecfg.get("live", False) else "模拟开仓"
            print(f"[{label}] {sym} {r['trade']}")
            print(f"[复盘] {json.dumps(self.paper.stats(), ensure_ascii=False)}")

    # --- 连续合成行情模拟盘：随机游走 + 偶发突破，跑起来看开平仓与复盘 ---
    def mock_run(self, steps: int = 0):
        import random
        random.seed(int(self.ecfg.get("sim_seed", 42)))
        tick = float(self.ecfg.get("mock_tick", 0.2))
        # 模拟盘把事件冷却调短，好看到多次决策
        self.cooldown = int(self.ecfg.get("mock_cooldown", 5))
        print(f"[mock] 连续合成行情模拟盘启动 (cooldown={self.cooldown}s, tick={tick}s)，Ctrl+C 退出")
        px = 0.2400
        ohlcv = []
        i = 0
        while steps == 0 or i < steps:
            drift = random.uniform(-0.0008, 0.0008)
            if random.random() < 0.10:   # 10% 概率放量突破
                drift = 0.0025
            px += drift
            ohlcv.append([i, px, px + 0.0005, px - 0.0005, px,
                          4000 if random.random() < 0.15 else 1000])
            ohlcv = ohlcv[-120:]
            raw = {"symbol": self.symbol, "ohlcv": ohlcv,
                   "orderbook": {"bids": [[px, 5000]], "asks": [[px + 0.0001, 2000]]},
                   "oi": 100000, "oi_change_5m_pct": 2.0 if random.random() < 0.2 else 0.0,
                   "funding": 0.00012}
            try:
                r = self.step(raw)
                snap = r.get("snapshot") or {}
                atr = snap.get("atr") or 0.0
                for sym in list(self.paper.positions.keys()):
                    self.settle_at(sym, px, atr)
                self._report(r)
            except Exception as e:
                print(f"[mock] 出错: {e}")
            i += 1
            time.sleep(tick)
        print("=== 模拟盘最终复盘 ===")
        print(json.dumps(self.paper.stats(), ensure_ascii=False, indent=2))
        for t in self.paper.trades:
            print(f"  交易: {t.side} {t.symbol} {t.entry}->{t.exit} {t.exit_reason} pnl={t.pnl}")


# ---------------------------------------------------------------------------
# 合成数据自测：构造一段「突破」行情跑通全链路
# ---------------------------------------------------------------------------
def simulate(cfg: dict):
    print("[simulate] 生成合成突破行情…")
    seed = float(cfg.get("engine", {}).get("sim_seed", 42))
    random.seed(seed)
    bars = []
    px = 0.2400
    for i in range(60):
        px += random.uniform(-0.0004, 0.0004)
        o, c = px, px + random.uniform(-0.0002, 0.0002)
        h, l = max(o, c) + 0.0003, min(o, c) - 0.0003
        v = random.uniform(800, 1200)
        bars.append([i, o, h, l, c, v])
    # 最后 6 根放量突破
    for i in range(60, 66):
        px += 0.0012
        o, c = px - 0.0005, px
        h, l = max(o, c) + 0.0004, min(o, c) - 0.0004
        v = random.uniform(3000, 5000)
        bars.append([i, o, h, l, c, v])

    raw = {"symbol": "TEST/USDT", "ohlcv": bars,
           "orderbook": {"bids": [[px, 5000], [px - 0.0001, 4000]],
                         "asks": [[px + 0.0001, 2000], [px + 0.0002, 1500]]},
           "oi": 100000, "oi_change_5m_pct": 2.5, "funding": 0.00012}

    eng = Engine(cfg)
    eng.symbol = "TEST/USDT"
    eng.exchange = None  # simulate 不用真实交易所
    r = eng.step(raw)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    print("\n[复盘统计]", json.dumps(eng.paper.stats(), ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="../config.yaml")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--mock", action="store_true", help="连续合成行情模拟盘（跑起来看开平仓）")
    ap.add_argument("--steps", type=int, default=0, help="mock 模式运行步数，0=无限")
    ap.add_argument("--ws", action="store_true", help="WebSocket 实时流模式")
    ap.add_argument("--tg", action="store_true", help="TG 信号驱动：信号选交易对，实时行情决定入场")
    ap.add_argument("--gainers", action="store_true", help="涨幅榜信号源：监控 24h 涨幅 Top N 交易对（方向=做多）")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, "r", encoding="utf-8"))
    if args.simulate:
        simulate(cfg)
    elif args.mock:
        Engine(cfg).mock_run(args.steps)
    elif args.gainers:
        asyncio.run(Engine(cfg).run_gainers())
    elif args.tg:
        asyncio.run(Engine(cfg).run_tg())
    elif args.ws:
        asyncio.run(Engine(cfg).run_ws())
    else:
        Engine(cfg).run()
