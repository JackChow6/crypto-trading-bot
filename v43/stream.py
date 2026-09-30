#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/stream.py —— 单币 WebSocket 实时行情流（Trade / OrderBook / Kline 三流）

用 ccxt.pro 订阅「单个交易对」的 WebSocket。要盯多个币，就为每个币各开一个
MarketStream（并发跑），由上层 engine 管理。连不上时标记 healthy=False，上层回退 REST。

依赖: pip install ccxtpro
"""
from __future__ import annotations

import asyncio
import time


class MarketStream:
    def __init__(self, exchange_name: str, symbol: str, cfg: dict | None = None):
        cfg = cfg or {}
        import ccxt.pro as ccxtpro
        params = {
            "enableRateLimit": True,
            "options": {"defaultType": cfg.get("default_type", "swap")},
        }
        if cfg.get("api_key"):
            params["apiKey"] = cfg["api_key"]
            params["secret"] = cfg["api_secret"]
        # ccxt.pro 的 REST 走 aiohttp_proxy；WS 走代理可能被墙，失败由上层降级
        if cfg.get("aiohttp_proxy"):
            params["aiohttp_proxy"] = cfg["aiohttp_proxy"]
        self.exchange = getattr(ccxtpro, exchange_name)(params)
        self.symbol = symbol
        self.timeframe = cfg.get("timeframe", "1m")
        self.klines: list = []
        self.orderbook: dict = {"bids": [], "asks": []}
        self.trades: list = []
        self.oi = None
        self.funding = None
        self.last_price: float | None = None
        self.healthy = True          # 连续失败则置 False，上层回退 REST
        self._fail = 0

    async def start(self):
        await self.exchange.load_markets()

    def _ok(self):
        self._fail = 0

    def _err(self, tag: str, e) -> bool:
        """返回 True 表示该流已不可用。"""
        self._fail += 1
        if self._fail >= 5:
            self.healthy = False
            print(f"[stream] {self.symbol} 连续失败，标记不可用(回退 REST): {tag}")
            return True
        print(f"[stream] {self.symbol} {tag} 出错(重连): {e}")
        return False

    async def _watch_ohlcv(self):
        while self.healthy:
            try:
                self.klines = await self.exchange.watch_ohlcv(self.symbol, self.timeframe)
                self._ok()
                if self.klines:
                    self.last_price = self.klines[-1][4]
            except Exception as e:
                if self._err("watch_ohlcv", e):
                    return
                await asyncio.sleep(3)

    async def _watch_orderbook(self):
        while self.healthy:
            try:
                self.orderbook = await self.exchange.watch_order_book(self.symbol, 20)
                self._ok()
            except Exception as e:
                if self._err("watch_orderbook", e):
                    return
                await asyncio.sleep(3)

    async def _watch_trades(self):
        while self.healthy:
            try:
                trades = await self.exchange.watch_trades(self.symbol)
                self._ok()
                self.trades.extend(trades)
                self.trades = self.trades[-500:]
                if trades:
                    last = trades[-1]
                    px = last[8] if isinstance(last, (list, tuple)) else last.get("price")
                    if px is not None:
                        self.last_price = float(px)
            except Exception as e:
                if self._err("watch_trades", e):
                    return
                await asyncio.sleep(3)

    async def _watch_deriv(self):
        """OI / funding，失败静默（并非所有所都支持）。"""
        while self.healthy:
            try:
                self.oi = await self.exchange.watch_open_interest(self.symbol)
            except Exception:
                pass
            try:
                self.funding = await self.exchange.watch_funding_rate(self.symbol)
            except Exception:
                pass
            await asyncio.sleep(30)

    async def run(self):
        """启动四路订阅（并发），直到被取消或标记不可用。"""
        await self.start()
        tasks = [asyncio.create_task(f()) for f in
                 (self._watch_ohlcv, self._watch_orderbook, self._watch_trades, self._watch_deriv)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    def snapshot(self) -> dict:
        """把当前缓冲打包成 features.build_snapshot 需要的 raw dict。"""
        return {
            "symbol": self.symbol,
            "ohlcv": self.klines,
            "orderbook": self.orderbook,
            "trades": self.trades,
            "oi": self.oi.get("openInterestAmount") if isinstance(self.oi, dict) else self.oi,
            "funding": self.funding,
        }

    async def close(self):
        try:
            await self.exchange.close()
        except Exception:
            pass


class MultiStream:
    """合并连接：一个 ccxt.pro 实例 + 3 路多币订阅(ohlcv/orderbook/trades)。

    用 watch_*_for_symbols 把 N 个交易对合并进同一条 WebSocket，
    把 ~N×4 条连接压缩到 ~3 条。symbol 集合变化时（涨幅榜刷新）自动重连并重订阅。

    对外提供与单币 MarketStream 一致的按 symbol 读取接口：
      last_price: dict[sym -> float]
      klines:     dict[sym -> list]（REST 预拉历史 + WS 增量合并）
      orderbooks: dict[sym -> {bids, asks}]
      trades:     dict[sym -> list]
      snapshot(sym) / book(sym)
    """

    def __init__(self, exchange_name: str, cfg: dict | None = None):
        cfg = cfg or {}
        self.exchange_name = exchange_name
        self.cfg = cfg
        self.timeframe = cfg.get("timeframe", "1m")   # K 线周期
        self.exchange = None
        self.symbols: list[str] = []
        self._gen = 0
        self.klines: dict[str, list] = {}
        self.orderbooks: dict[str, dict] = {}
        self.trades: dict[str, list] = {}
        self.last_price: dict[str, float] = {}
        self.last_price_ts: dict[str, float] = {}   # 每个 symbol 最新价更新时间，用于判断是否过期
        self.healthy = True
        self._fail = 0

    def _build_exchange(self):
        import ccxt.pro as ccxtpro
        params = {
            "enableRateLimit": True,
            "options": {"defaultType": self.cfg.get("default_type", "swap")},
        }
        if self.cfg.get("api_key"):
            params["apiKey"] = self.cfg["api_key"]
            params["secret"] = self.cfg["api_secret"]
        if self.cfg.get("aiohttp_proxy"):
            params["aiohttp_proxy"] = self.cfg["aiohttp_proxy"]
        return getattr(ccxtpro, self.exchange_name)(params)

    def set_symbols(self, symbols: list[str]) -> bool:
        """更新订阅集合；与当前集合不同时返回 True 并触发重连。"""
        new = list(dict.fromkeys(s for s in symbols if s))
        if new == self.symbols:
            return False
        self.symbols = new
        self._gen += 1
        print(f"[stream] 合并订阅 {len(new)} 个交易对")
        return True

    def _ok(self):
        self._fail = 0

    def _err(self, tag: str, e) -> bool:
        self._fail += 1
        if self._fail >= 5:
            self.healthy = False
            print(f"[stream] 合并连接连续失败，标记不可用(回退 REST): {tag}")
            return True
        print(f"[stream] 合并 {tag} 出错(重连): {e}")
        return False

    @staticmethod
    def _merge_ohlcv(sym: str, base: list, ws: list, cap: int = 200) -> list:
        """把 WS 增量 K 线按时间戳合并进 REST 预拉的历史，去重并裁到 cap。"""
        idx = {c[0]: c for c in base}
        for c in ws:
            try:
                idx[int(c[0])] = c
            except (TypeError, ValueError, IndexError):
                continue
        return sorted(idx.values(), key=lambda c: c[0])[-cap:]

    async def _watch_ohlcv(self, syms):
        pairs = [[s, self.timeframe] for s in syms]
        while self.healthy and self.exchange is not None:
            try:
                res = await self.exchange.watch_ohlcv_for_symbols(pairs)
                self._ok()
                # res: {symbol: {timeframe: candles}}，一次只返回一个 symbol 的更新
                if isinstance(res, dict):
                    for sym, tfmap in res.items():
                        if isinstance(tfmap, dict):
                            for _, candles in tfmap.items():
                                if candles:
                                    merged = self._merge_ohlcv(sym, self.klines.get(sym, []), candles)
                                    self.klines[sym] = merged
                                    self.last_price[sym] = float(merged[-1][4])
                                    self.last_price_ts[sym] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._err("watch_ohlcv", e):
                    return
                await asyncio.sleep(3)

    async def _watch_orderbook(self, syms):
        while self.healthy and self.exchange is not None:
            try:
                ob = await self.exchange.watch_order_book_for_symbols(syms, 20)
                self._ok()
                sym = ob.get("symbol") if isinstance(ob, dict) else None
                if sym:
                    self.orderbooks[sym] = {"bids": ob.get("bids", []), "asks": ob.get("asks", [])}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._err("watch_orderbook", e):
                    return
                await asyncio.sleep(3)

    async def _watch_trades(self, syms):
        while self.healthy and self.exchange is not None:
            try:
                trades = await self.exchange.watch_trades_for_symbols(syms)
                self._ok()
                if trades:
                    for t in trades:
                        if not isinstance(t, dict):
                            continue
                        sym = t.get("symbol")
                        if not sym:
                            continue
                        buf = self.trades.setdefault(sym, [])
                        buf.append(t)
                        self.trades[sym] = buf[-500:]
                        px = t.get("price")
                        if px is not None:
                            self.last_price[sym] = float(px)
                            self.last_price_ts[sym] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._err("watch_trades", e):
                    return
                await asyncio.sleep(3)

    async def run(self):
        """监督循环：symbol 集合变化时取消旧订阅、关闭连接、按新集合重连。"""
        tasks: list = []
        try:
            while True:
                if not self.symbols:
                    await asyncio.sleep(2)
                    continue
                if self.exchange is None:
                    self.exchange = self._build_exchange()
                    await self.exchange.load_markets()
                gen = self._gen
                syms = list(self.symbols)
                # REST 预拉历史 K 线，避免 WS 增量从 0 开始攒（否则 ATR/EMA 长期不可用）
                # 只预拉「新加入」的交易对，已在 klines 里的由 WS 增量继续维护（减少 REST，避免限流）
                for sym in syms:
                    if sym in self.klines:
                        continue
                    try:
                        ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, limit=120)
                        if ohlcv:
                            self.klines[sym] = ohlcv[-200:]
                            self.last_price[sym] = float(ohlcv[-1][4])
                            self.last_price_ts[sym] = time.time()
                    except Exception:
                        pass
                tasks = [
                    asyncio.create_task(self._watch_ohlcv(syms)),
                    asyncio.create_task(self._watch_orderbook(syms)),
                    asyncio.create_task(self._watch_trades(syms)),
                ]
                last_hb = time.time()
                while self.healthy and self._gen == gen:
                    await asyncio.sleep(1)
                    if time.time() - last_hb >= 60:
                        fresh = sum(1 for s, ts in self.last_price_ts.items() if time.time() - ts < 60)
                        print(f"[stream] 心跳 healthy={self.healthy} 新鲜价={fresh}/{len(self.last_price_ts)} "
                              f"trades缓冲={sum(len(v) for v in self.trades.values())}", flush=True)
                        last_hb = time.time()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await self.exchange.close()
                except Exception:
                    pass
                self.exchange = None
                if not self.healthy:
                    await asyncio.sleep(5)
                    self.healthy = True   # 允许自动恢复重连
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if self.exchange is not None:
                try:
                    await self.exchange.close()
                except Exception:
                    pass
                self.exchange = None

    def snapshot(self, sym: str) -> dict:
        ob = self.orderbooks.get(sym) or {}
        return {
            "symbol": sym,
            "ohlcv": self.klines.get(sym, []),
            "orderbook": {"bids": ob.get("bids", []), "asks": ob.get("asks", [])},
            "trades": self.trades.get(sym, []),
            "oi": None,
            "funding": None,
            "last": self.last_price.get(sym),
        }

    def book(self, sym: str) -> tuple[list, list]:
        ob = self.orderbooks.get(sym) or {}
        return ob.get("bids", []), ob.get("asks", [])

    async def close(self):
        try:
            await self.exchange.close()
        except Exception:
            pass
