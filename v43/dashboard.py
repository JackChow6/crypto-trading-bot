#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/dashboard.py —— Web 控制台（专业交易面板风格）

引擎进程内嵌 aiohttp 服务，浏览器打开 http://127.0.0.1:8000：
- 概览：权益/余额、今日盈亏、净盈亏、总浮盈、胜率、盈亏比、最大回撤
- 动量/涨幅榜单（各周期收益 + 成交额 + 是否已持仓）
- K线图（蜡烛 + 成交量 + MA 均线 + 入场/止盈/止损线 + 十字光标）
- 权益曲线（渐变填充 + 峰值）
- 持仓卡片（浮盈% + SL→TP 进度条 + 持仓时长）
- 成交 / 订单 / 监控列表 / 决策
- 操作：暂停·恢复、清空、手动加币、移除、平仓、全平、熔断/解除

数据源优先 WebSocket 实时价 + 引擎内存缓存，不逐币打 REST，避免限流。
依赖: aiohttp（ccxtpro 已自带）
"""
from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web


class Dashboard:
    def __init__(self, engine, host: str = "127.0.0.1", port: int = 8000):
        self.engine = engine
        self.host = host
        self.port = port
        self.started_at = time.time()
        self.paused = False
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/state", self._state)
        self.app.router.add_get("/api/candles", self._candles)
        self.app.router.add_post("/api/action", self._action)

    # ---- 实时价（WS 优先，内存缓存兜底，不逐币打 REST）----
    def _price(self, sym):
        px = self.engine._stream_price(sym)
        if px:
            return px
        return getattr(self.engine, "last_prices", {}).get(sym)

    def _ws_healthy(self):
        st = getattr(self.engine, "streams", None)
        if st is None:
            return False
        if isinstance(st, dict):
            return bool(st) and all(getattr(m, "healthy", False) for m in st.values())
        return bool(getattr(st, "healthy", False))

    async def _state(self, request):
        e = self.engine
        now = time.time()

        # 持仓：用 WS/内存实时价，不逐币 fetch_price（避免 3s 刷屏打爆 REST 限流）
        positions = []
        unrealized = 0.0
        notional_total = 0.0
        long_n = short_n = 0
        for sym, plist in e.paper.positions.items():
            px = self._price(sym)
            for p in plist:
                px = px or p.entry
                pnl = p.unrealized_pnl(px)
                unrealized += pnl
                notional = p.size_usd * p.leverage
                notional_total += notional
                if p.side == "long":
                    long_n += 1
                else:
                    short_n += 1
                held_s = int(now - p.opened_at)
                roe = round(pnl / p.size_usd * 100, 2) if p.size_usd else 0.0
                pnl_pct = round(pnl / notional * 100, 4) if notional else 0.0
                # 距止损/止盈百分比（相对当前价）
                dist_sl = dist_tp = None
                if px and p.sl:
                    dist_sl = round(abs(px - p.sl) / px * 100, 3)
                if px and p.tp:
                    dist_tp = round(abs(p.tp - px) / px * 100, 3)
                positions.append({
                    "symbol": sym, "side": p.side, "entry": p.entry, "sl": p.sl, "tp": p.tp,
                    "size_usd": p.size_usd, "leverage": p.leverage,
                    "pnl": round(pnl, 2), "price": px, "pnl_pct": pnl_pct, "roe": roe,
                    "held_s": held_s, "notional": round(notional, 2),
                    "dist_sl_pct": dist_sl, "dist_tp_pct": dist_tp,
                })

        # 榜单：动量/涨幅榜完整数据（含各周期收益、成交额、是否已持仓）
        board = []
        for g in getattr(e, "gainer_board", []):
            sym = g.get("symbol", "")
            px = self._price(sym)
            plist = e.paper.positions.get(sym)
            pos_side = plist[0].side if plist else None
            board.append({
                "symbol": sym,
                "change_pct": g.get("change_pct"),
                "last": g.get("last"),
                "price": px if px else g.get("last"),
                "quote_volume": g.get("quote_volume"),
                "rets": g.get("rets") or {},
                "in_positions": bool(plist),
                "pos_side": pos_side,
            })

        live_trades = [{"symbol": t.symbol, "side": t.side, "entry": round(t.entry, 6),
                        "exit": round(t.exit, 6), "pnl": t.pnl, "leverage": getattr(t, "leverage", 1),
                        "reason": t.exit_reason, "closed_at": getattr(t, "closed_at", 0)}
                       for t in e.paper.trades[-50:]][::-1]
        hist_trades = []
        live_keys = {(t["symbol"], round(t["entry"], 6), round(t["exit"], 6), t["pnl"]) for t in live_trades}
        for t in reversed(getattr(e, "history_trades", [])):
            try:
                key = (t["symbol"], round(t["entry"], 6), round(t["exit"], 6), t["pnl"])
            except Exception:
                continue
            if key in live_keys:
                continue
            hist_trades.append({"symbol": t["symbol"], "side": t["side"],
                                "entry": round(t["entry"], 6), "exit": round(t["exit"], 6),
                                "pnl": t["pnl"], "leverage": 10, "hist": True,
                                "reason": t["reason"], "closed_at": t.get("ts", 0)})
        trades = hist_trades + live_trades

        fills = [{"order_id": f.order_id, "symbol": f.symbol, "side": f.side,
                  "price": round(f.price, 8), "qty": round(f.qty, 8), "ts": f.ts}
                 for f in e.paper.matcher.fills[-30:]][::-1]
        orders = [{"order_id": o.order_id, "symbol": o.symbol, "side": o.side, "type": o.type,
                   "notional": round(o.notional, 4), "status": o.status,
                   "avg_price": round(o.avg_price, 8), "filled_qty": round(o.filled_qty, 8)}
                  for o in e.paper.matcher.orders[-15:]][::-1]
        risk = e.risk.state
        stats = e.paper.stats()
        decisions = getattr(e, "decision_history", [])[-12:][::-1]
        equity_curve = getattr(e, "equity_history", [])

        # 榜单类型（momentum=动量分 / gainers=24h 涨幅）
        pool_type = str((e.ecfg.get("gainers") or {}).get("signal_pool", "momentum")).lower()
        direction = str((e.ecfg.get("gainers") or {}).get("direction", "both")).lower()
        live_mode = bool(e.ecfg.get("live", False))

        watchlist = []
        for s, i in e.watchlist.items():
            watchlist.append({"symbol": s, "side": i.get("side"), "summary": i.get("summary", "")})

        return web.json_response({
            "running": True, "paused": self.paused, "live": live_mode,
            "uptime_s": int(time.time() - self.started_at),
            "server_ts": time.time(),
            "signal_source": e.ecfg.get("signal_source", "rule"),
            "pool_type": pool_type, "direction": direction,
            "ws_healthy": self._ws_healthy(),
            "balance": round(e.paper.equity, 2),
            "summary": {
                "open_count": len(positions),
                "unrealized": round(unrealized, 2),
                "notional": round(notional_total, 2),
                "long_n": long_n, "short_n": short_n,
            },
            "watchlist": watchlist, "positions": positions, "trades": trades,
            "stats": stats, "board": board,
            "orders": orders, "fills": fills, "decisions": decisions, "equity_curve": equity_curve,
            "risk": {"equity": round(risk.equity, 2), "daily_pnl": round(risk.daily_pnl, 2),
                     "open_positions": risk.open_positions, "killed": risk.killed,
                     "reason": risk.reason},
        })

    # ---- K线 ----
    async def _candles(self, request):
        sym = request.query.get("symbol", "")
        tf = request.query.get("timeframe", getattr(self.engine, "timeframe", "1m"))
        e = self.engine
        candles = await asyncio.to_thread(e.fetch_candles, sym, tf)
        plist = e.paper.positions.get(sym)
        pos = plist[0] if plist else None
        trades = [t for t in e.paper.trades if t.symbol == sym][-10:]
        return web.json_response({
            "symbol": sym, "timeframe": tf, "candles": candles,
            "position": None if pos is None else {
                "side": pos.side, "entry": pos.entry, "sl": pos.sl, "tp": pos.tp,
                "opened_at": pos.opened_at * 1000,
            },
            "trades": [{"side": t.side, "entry": t.entry, "exit": t.exit,
                        "opened_at": t.opened_at * 1000, "closed_at": t.closed_at * 1000,
                        "reason": t.exit_reason} for t in trades],
        })

    # ---- 操作 ----
    async def _action(self, request):
        data = {}
        try:
            text = await request.text()
            if text:
                data = json.loads(text)
        except Exception:
            data = {}
        act = data.get("action")
        e = self.engine

        def ok(msg, **kw):
            return web.json_response({"ok": True, "msg": msg, **kw})

        if act == "pause":
            self.paused = True
            return ok("已暂停（不再开新仓，持仓仍结算）")
        if act == "resume":
            self.paused = False
            return ok("已恢复")
        if act == "clear_paper":
            e.paper.trades.clear()
            e.paper.positions.clear()
            e.paper.equity = float(e.ecfg.get("paper", {}).get("initial_equity", 1000))
            e.paper.peak_equity = e.paper.equity
            e.risk.state.equity = float(e.ecfg.get("risk", {}).get("initial_equity", 1000))
            e.risk.state.peak_equity = e.risk.state.equity
            e.risk.state.daily_pnl = 0.0
            e.risk.state.open_positions = 0
            e.paper._save()
            return ok("模拟盘已清空")
        if act == "add_symbol":
            sym = (data.get("symbol") or "").strip().upper()
            if not sym:
                return web.json_response({"ok": False, "msg": "symbol 为空"})
            if ":" not in sym and "/" in sym:
                sym = f"{sym}:{sym.split('/')[1]}"
            e.watchlist[sym] = {"side": data.get("side", "buy"),
                                "summary": f"[手动] {sym} {data.get('side','buy')}"}
            e._save_watchlist()
            return ok(f"已加入监控 {sym}")
        if act == "remove_symbol":
            sym = (data.get("symbol") or "").strip().upper()
            e.watchlist.pop(sym, None)
            e._save_watchlist()
            return ok(f"已移除 {sym}")
        if act == "close_position":
            sym = (data.get("symbol") or "").strip().upper()
            if not sym:
                return web.json_response({"ok": False, "msg": "symbol 为空"})
            closed = await asyncio.to_thread(e.manual_close, sym)
            if closed:
                return ok(f"已平仓 {sym}，盈亏={closed.pnl}")
            return web.json_response({"ok": False, "msg": f"{sym} 无持仓"})
        if act == "close_all":
            n = await asyncio.to_thread(e.close_all_positions, "MANUAL_CLOSE_ALL")
            return ok(f"已平仓 {n} 个持仓")
        if act == "kill":
            e.risk.state.killed = True
            e.risk.state.reason = "手动熔断"
            return ok("已手动熔断（停止开新仓）")
        if act == "unkill":
            e.risk.state.killed = False
            e.risk.state.reason = ""
            return ok("已解除熔断")
        return web.json_response({"ok": False, "msg": f"未知操作: {act}"})

    async def _index(self, request):
        tf = getattr(self.engine, "timeframe", "1m")
        html = HTML.replace("curTf='1m'", f"curTf='{tf}'")
        html = html.replace(f'<option value="{tf}">', f'<option value="{tf}" selected>')
        return web.Response(text=html, content_type="text/html")

    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print(f"[dashboard] http://{self.host}:{self.port}")


HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>交易引擎 · 控制台</title>
<style>
:root{
  --bg:#0b0e14;--bg2:#0e1220;--panel:#131722;--panel2:#1a1f2e;--bd:#232936;--bd2:#2e3648;
  --fg:#e8eef6;--dim:#8b99ad;--dim2:#5f6f84;
  --up:#0ecb81;--down:#f6465d;--accent:#f0b90b;--blue:#3b82f6;--purple:#a78bfa;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#2e3648;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* ===== Header ===== */
header{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 18px;height:56px;background:rgba(11,14,20,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd)}
.hd-left{display:flex;align-items:center;gap:12px;min-width:0}
.logo{display:flex;align-items:center;gap:8px;font-weight:800;font-size:16px;letter-spacing:.3px;white-space:nowrap}
.logo .mark{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#f0b90b,#f6465d);display:inline-flex;align-items:center;justify-content:center;font-size:13px;color:#0b0e14;font-weight:900}
.hd-right{display:flex;align-items:center;gap:14px;color:var(--dim);font-size:12px;flex-shrink:0}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;font-size:12px;font-weight:600}
.badge .dot{width:7px;height:7px;border-radius:50%}
.badge.on{background:rgba(14,203,129,.14);color:var(--up)} .badge.on .dot{background:var(--up);box-shadow:0 0 8px var(--up)}
.badge.pause{background:rgba(240,185,11,.14);color:var(--accent)} .badge.pause .dot{background:var(--accent)}
.badge.kill{background:rgba(246,70,93,.14);color:var(--down)} .badge.kill .dot{background:var(--down)}
.badge.ws{background:rgba(59,130,246,.14);color:var(--blue)} .badge.ws .dot{background:var(--blue)}
.badge.wsoff{background:rgba(95,111,132,.16);color:var(--dim2)} .badge.wsoff .dot{background:var(--dim2)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,"Cascadia Mono",monospace;font-variant-numeric:tabular-nums}

/* ===== Layout ===== */
.wrap{padding:14px 18px;max-width:1680px;margin:0 auto}

/* ===== KPI cards ===== */
.grid{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin-bottom:14px}
@media(max-width:1400px){.grid{grid-template-columns:repeat(4,1fr)}}
@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}}
.card{background:linear-gradient(180deg,#161b29,#10141f);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;position:relative;overflow:hidden}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.3px}
.card .v{font-size:20px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums;letter-spacing:-.4px}
.card .sub{font-size:11px;color:var(--dim2);margin-top:1px}
.pos{color:var(--up)} .neg{color:var(--down)} .amber{color:var(--accent)} .blue{color:var(--blue)}

/* ===== Panels ===== */
.panel{background:var(--panel);border:1px solid var(--bd);border-radius:12px;padding:14px;margin-bottom:14px}
.panel h2{font-size:12px;font-weight:700;margin-bottom:12px;color:#c7d2e0;letter-spacing:.4px;text-transform:uppercase;display:flex;align-items:center;gap:8px}
.panel h2 .cnt{color:var(--dim2);font-weight:500;text-transform:none}
.panel h2 .sp{flex:1}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
button{background:var(--blue);color:#fff;border:none;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:12px;font-weight:600;transition:.15s;font-family:inherit}
button:hover{filter:brightness(1.15)}
button.ghost{background:transparent;border:1px solid var(--bd2);color:var(--fg)}
button.danger{background:rgba(246,70,93,.15);color:var(--down);border:1px solid rgba(246,70,93,.35)}
button.warn{background:rgba(240,185,11,.14);color:var(--accent);border:1px solid rgba(240,185,11,.35)}
button.small{padding:4px 9px;font-size:11px;border-radius:6px}
select,input{background:var(--panel2);border:1px solid var(--bd2);border-radius:8px;padding:7px 10px;color:var(--fg);font-size:12px;outline:none;font-family:inherit}
select:focus,input:focus{border-color:var(--accent)}
canvas{width:100%;display:block;background:var(--panel2);border-radius:10px}
#chart{height:360px}
#equityChart{height:140px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd);font-size:12px;white-space:nowrap}
th{color:var(--dim2);font-weight:600;font-size:10px;letter-spacing:.5px;text-transform:uppercase;background:rgba(255,255,255,.02);position:sticky;top:0}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:rgba(59,130,246,.05)}
tr:last-child td{border-bottom:none}
.long{color:var(--up)} .short{color:var(--down)}
.tbl-scroll{max-height:360px;overflow:auto}
.legend{display:flex;gap:16px;font-size:11px;color:var(--dim);margin-top:8px;flex-wrap:wrap}
.legend i{display:inline-block;width:16px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}

/* ===== Tabs ===== */
.tabs{display:flex;gap:2px;margin-bottom:12px;border-bottom:1px solid var(--bd);overflow-x:auto}
.tab{padding:9px 16px;background:none;border:none;color:var(--dim);cursor:pointer;font-size:13px;font-weight:600;border-bottom:2px solid transparent;border-radius:0;white-space:nowrap}
.tab:hover{color:var(--fg)}
.tab.active{color:var(--fg);border-bottom-color:var(--accent)}
.tabpage{display:none}
.tabpage.active{display:block}

/* ===== Two-column body ===== */
.body-grid{display:grid;grid-template-columns:1fr 340px;gap:14px;align-items:start}
@media(max-width:1100px){.body-grid{grid-template-columns:1fr}}

/* ===== Board table ===== */
.board-sym{font-weight:700}
.board-coin{font-size:11px;color:var(--dim2)}
.pct-bar{display:inline-block;height:5px;border-radius:3px;vertical-align:middle}
.ret-cell{font-size:11px;color:var(--dim)}
.ret-up{color:var(--up)} .ret-down{color:var(--down)}

/* ===== Position cards ===== */
.pos-card{background:var(--panel2);border:1px solid var(--bd);border-radius:10px;padding:11px 13px;margin-bottom:9px;border-left:3px solid var(--blue)}
.pos-card.long{border-left-color:var(--up)}
.pos-card.short{border-left-color:var(--down)}
.pos-head{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.pos-head .sym{font-weight:800;font-size:13px}
.pos-head .side-tag{padding:1px 8px;border-radius:5px;font-size:10px;font-weight:700}
.pos-head .side-tag.long{background:rgba(14,203,129,.15);color:var(--up)}
.pos-head .side-tag.short{background:rgba(246,70,93,.15);color:var(--down)}
.pos-head .lev{font-size:10px;color:var(--dim2);border:1px solid var(--bd2);border-radius:5px;padding:0 6px}
.pos-head .sp{flex:1}
.pos-pnl{font-weight:800;font-size:14px}
.pos-row{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:3px}
.pos-row b{color:var(--fg);font-weight:600}
.pos-bar{height:6px;border-radius:4px;background:var(--bd);position:relative;margin:8px 0 4px;overflow:hidden}
.pos-bar .fill{position:absolute;top:0;bottom:0;border-radius:4px}
.pos-bar .marker{position:absolute;top:-2px;bottom:-2px;width:2px;background:#fff;border-radius:1px}
.pos-meta{display:flex;justify-content:space-between;font-size:10px;color:var(--dim2)}
.held{font-size:11px;color:var(--dim)}

/* ===== Chart tooltip ===== */
#chartTip{position:absolute;pointer-events:none;background:rgba(11,14,20,.95);border:1px solid var(--bd2);border-radius:8px;padding:8px 10px;font-size:11px;display:none;z-index:10;font-family:ui-monospace,Consolas,monospace}
.chart-wrap{position:relative}

.empty{color:var(--dim2);text-align:center;padding:20px 0;font-size:12px}
.pulse{animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.fade-in{animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
</style></head><body>

<header>
  <div class="hd-left">
    <div class="logo"><span class="mark">▚</span><span>交易引擎</span></div>
    <span id="status" class="badge on"><span class="dot"></span>运行中</span>
    <span id="wsBadge" class="badge ws"><span class="dot"></span>WS 实时</span>
  </div>
  <div class="hd-right">
    <span id="clock" class="mono">--:--:--</span>
    <span>运行 <b id="uptime" class="mono">0s</b></span>
    <span id="refreshNote" class="dim">3s 自动刷新</span>
  </div>
</header>

<div class="wrap">

<div class="grid">
  <div class="card"><div class="k">权益 / 余额</div><div class="v" id="equity">--</div><div class="sub" id="equitySub">--</div></div>
  <div class="card"><div class="k">今日盈亏</div><div class="v" id="daily">--</div></div>
  <div class="card"><div class="k">净盈亏</div><div class="v" id="netpnl">--</div></div>
  <div class="card"><div class="k">总浮盈</div><div class="v" id="unrealized">--</div></div>
  <div class="card"><div class="k">持仓</div><div class="v" id="posnum">--</div><div class="sub" id="posSub">--</div></div>
  <div class="card"><div class="k">胜率</div><div class="v" id="winrate">--</div></div>
  <div class="card"><div class="k">盈亏比</div><div class="v" id="pf">--</div></div>
  <div class="card"><div class="k">最大回撤</div><div class="v" id="dd">--</div></div>
</div>

<div class="toolbar">
  <button onclick="act('pause')">⏸ 暂停</button>
  <button onclick="act('resume')">▶ 恢复</button>
  <button class="warn" onclick="act('kill')">🛑 熔断</button>
  <button onclick="act('unkill')">解除熔断</button>
  <button class="danger" onclick="if(confirm('平掉所有持仓？'))act('close_all')">平全部</button>
  <button class="danger" onclick="if(confirm('清空模拟盘记账？'))act('clear_paper')">清空</button>
  <span style="flex:1"></span>
  <input id="sym" placeholder="BAT/USDT" style="width:120px">
  <select id="side"><option value="buy">多</option><option value="sell">空</option></select>
  <button class="ghost" onclick="addSym()">加入监控</button>
  <button class="ghost" onclick="refresh()">↻ 刷新</button>
</div>

<div class="body-grid">
  <div>
    <div class="panel">
      <div class="chart-bar" style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
        <h2 style="margin:0">K线图</h2>
        <select id="symSel" onchange="onSymChange()"></select>
        <select id="tfSel" onchange="onTfChange()">
          <option value="1m">1分</option>
          <option value="5m">5分</option>
          <option value="15m">15分</option>
          <option value="1h">1时</option>
          <option value="4h">4时</option>
          <option value="1d">1日</option>
        </select>
        <span class="dim" id="chartPos" style="font-size:12px">—</span>
      </div>
      <div class="chart-wrap"><canvas id="chart"></canvas><div id="chartTip"></div></div>
      <div class="legend">
        <span><i style="background:var(--blue)"></i>入场</span>
        <span><i style="background:var(--up)"></i>止盈 TP</span>
        <span><i style="background:var(--down)"></i>止损 SL</span>
        <span><i style="background:#f0b90b"></i>MA7</span>
        <span><i style="background:#a78bfa"></i>MA25</span>
        <span class="dim">· 鼠标悬停看价</span>
      </div>
    </div>
    <div class="panel"><h2>权益曲线 <span class="cnt" id="eqPeak"></span></h2>
    <canvas id="equityChart"></canvas></div>
  </div>

  <div>
    <div class="panel"><h2>持仓 <span class="cnt" id="posCardCnt"></span></h2>
      <div id="posCards"><div class="empty">无持仓</div></div>
    </div>
    <div class="panel"><h2 id="boardTitle">动量榜</h2>
      <div class="tbl-scroll" style="max-height:420px">
      <table><thead><tr><th>#</th><th>币种</th><th>分/涨幅</th><th>5m</th><th>15m</th><th>30m</th><th>1h</th><th>4h</th><th>成交额</th><th>现价</th></tr></thead>
      <tbody id="board"><tr><td colspan="10" class="dim">加载中…</td></tr></tbody></table>
      </div>
    </div>
  </div>
</div>

<div class="tabs">
  <button class="tab active" data-tab="trade">成交 <span class="dim" id="tradeTabCnt"></span></button>
  <button class="tab" data-tab="wl">监控列表 <span class="dim" id="wlTabCnt"></span></button>
  <button class="tab" data-tab="order">订单</button>
  <button class="tab" data-tab="ai">信号决策</button>
</div>

<div class="tabpage active" id="tab-trade">
  <div class="panel"><h2>成交记录 Trades</h2>
  <div class="tbl-scroll" style="max-height:420px">
  <table><thead><tr><th>交易对</th><th>方向</th><th>杠杆</th><th>入场</th><th>出场</th><th>盈亏</th><th>原因</th></tr></thead>
  <tbody id="trades"><tr><td colspan="7" class="dim">暂无成交</td></tr></tbody></table></div></div>
  <div class="panel"><h2>撮合成交 Fills</h2>
  <div class="tbl-scroll" style="max-height:260px">
  <table><thead><tr><th>订单号</th><th>交易对</th><th>方向</th><th>成交价</th><th>数量</th></tr></thead>
  <tbody id="fills"><tr><td colspan="5" class="dim">暂无撮合成交</td></tr></tbody></table></div></div>
</div>

<div class="tabpage" id="tab-wl">
  <div class="panel"><h2>监控列表 Watchlist</h2>
  <div class="tbl-scroll" style="max-height:420px">
  <table><thead><tr><th>交易对</th><th>方向</th><th>信号</th><th></th></tr></thead>
  <tbody id="watchlist"><tr><td colspan="4" class="dim">等待信号…</td></tr></tbody></table></div></div>
</div>

<div class="tabpage" id="tab-order">
  <div class="panel"><h2>订单 Orders</h2>
  <div class="tbl-scroll" style="max-height:360px">
  <table><thead><tr><th>订单号</th><th>交易对</th><th>方向</th><th>类型</th><th>名义</th><th>状态</th><th>成交均价</th></tr></thead>
  <tbody id="orders"><tr><td colspan="7" class="dim">暂无订单</td></tr></tbody></table></div></div>
</div>

<div class="tabpage" id="tab-ai">
  <div class="panel"><h2>信号决策记录</h2>
  <div class="tbl-scroll" style="max-height:360px">
  <table><thead><tr><th>交易对</th><th>方向</th><th>信心</th><th>风险</th><th>看多</th><th>看空</th><th>理由</th></tr></thead>
  <tbody id="decisions"><tr><td colspan="7" class="dim">暂无决策</td></tr></tbody></table></div></div>
</div>

</div>

<script>
let curSym=null,curTf='1m';
let lastCandles=null, lastPos=null;
const fmt=(x,d=2)=>(x===null||x===undefined||isNaN(x))?'--':Number(x).toFixed(d);
function smart(x){ // 智能精度：币价差异大（BTC 10万 vs CRV 0.4）
  if(x===null||x===undefined||isNaN(x))return '--';
  const a=Math.abs(x);
  if(a>=1000)return x.toFixed(1);
  if(a>=100)return x.toFixed(2);
  if(a>=1)return x.toFixed(4);
  if(a>=0.01)return x.toFixed(5);
  return x.toFixed(7);
}
const pnlCls=v=>v>0?'pos':(v<0?'neg':'');
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function coin(s){return String(s).split('/')[0];}
function volFmt(v){ // 成交额缩写
  if(v===null||v===undefined)return '--';
  if(v>=1e9)return (v/1e9).toFixed(2)+'B';
  if(v>=1e6)return (v/1e6).toFixed(2)+'M';
  if(v>=1e3)return (v/1e3).toFixed(1)+'K';
  return v.toFixed(0);
}
function heldFmt(s){ // 持仓时长
  if(!s)return '--';
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h '+(Math.floor(s/60)%60)+'m';
  return Math.floor(s/86400)+'d '+(Math.floor(s/3600)%24)+'h';
}
function tsFmt(ts){ // 成交时间
  if(!ts)return '';
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}

/* ===== K线图 ===== */
function drawChart(sym,tf){
  if(!sym)return;
  fetch('/api/candles?symbol='+encodeURIComponent(sym)+'&timeframe='+(tf||'1m')).then(r=>r.json()).then(d=>{
    lastCandles=d.candles||[]; lastPos=d.position;
    const cv=document.getElementById('chart');
    const ctx=cv.getContext('2d');
    const w=cv.clientWidth, h=cv.clientHeight||300;
    const dpr=window.devicePixelRatio||1;
    cv.width=w*dpr; cv.height=h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,w,h);
    const cs=lastCandles.slice(-90);
    if(!cs.length){ctx.fillStyle='#7d8ba0';ctx.font='13px sans-serif';ctx.fillText('无数据',12,24);return;}
    const pos=lastPos;
    const volH=h*0.18;         // 底部成交量高度
    const mainH=h-volH-24;      // 主图高度
    const topPad=8;

    let lo=Infinity,hi=-Infinity,vmax=0;
    for(const c of cs){lo=Math.min(lo,c[3]);hi=Math.max(hi,c[2]);vmax=Math.max(vmax,c[5]||0);}
    if(pos){lo=Math.min(lo,pos.entry,pos.sl||lo);hi=Math.max(hi,pos.entry,pos.tp||hi);}
    const pad=(hi-lo)*0.05||1; lo-=pad; hi+=pad;
    const Y=p=>topPad+(mainH-((p-lo)/(hi-lo))*mainH);
    const cw=w/cs.length;

    // 网格 + 价格轴
    ctx.font='10px ui-monospace,Consolas,monospace';
    for(let g=0;g<=5;g++){
      const p=lo+(hi-lo)*g/5, y=Y(p);
      ctx.strokeStyle='rgba(255,255,255,.045)';ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();
      ctx.fillStyle='#5f6f84';ctx.textAlign='right';ctx.fillText(smart(p),w-4,y-3);
    }

    // 蜡烛 + 成交量
    for(let i=0;i<cs.length;i++){
      const c=cs[i],o=c[1],cl=c[4],hh=c[2],ll=c[3],v=c[5]||0;
      const x=i*cw+cw/2, col=cl>=o?'#0ecb81':'#f6465d';
      const bw=Math.max(1,cw*0.62);
      ctx.strokeStyle=col;ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(x,Y(hh));ctx.lineTo(x,Y(ll));ctx.stroke();
      const bt=Y(Math.max(o,cl)), bh=Math.max(1,Math.abs(Y(o)-Y(cl)));
      ctx.fillStyle=col;ctx.fillRect(x-bw/2,bt,bw,bh);
      // 成交量柱
      const vh=v/vmax*volH;
      ctx.fillStyle=col;ctx.globalAlpha=0.45;
      ctx.fillRect(x-bw/2,h-8-vh,bw,vh);
      ctx.globalAlpha=1;
    }

    // MA 均线
    function ma(n,color){
      ctx.strokeStyle=color;ctx.lineWidth=1.2;ctx.beginPath();
      let started=false;
      for(let i=n-1;i<cs.length;i++){
        let s=0;for(let j=i-n+1;j<=i;j++)s+=cs[j][4];const m=s/n;
        const x=i*cw+cw/2,y=Y(m);
        if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);
      }
      ctx.stroke();
    }
    ma(7,'#f0b90b');ma(25,'#a78bfa');

    // 最新价虚线
    const last=cs[cs.length-1][4];
    ctx.setLineDash([2,3]);ctx.strokeStyle='#7d8ba0';
    ctx.beginPath();ctx.moveTo(0,Y(last));ctx.lineTo(w,Y(last));ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='#7d8ba0';ctx.fillText(' '+smart(last),6,Y(last)-3);

    // 时间轴
    ctx.fillStyle='#5f6f84';ctx.textAlign='left';
    for(let i=0;i<cs.length;i+=Math.ceil(cs.length/6)){
      const d=new Date(cs[i][0]);
      const p=n=>String(n).padStart(2,'0');
      ctx.fillText(p(d.getHours())+':'+p(d.getMinutes()),i*cw+2,h-4);
    }

    // 持仓线
    function line(price,color,label){
      const y=Y(price);
      ctx.setLineDash([6,4]);ctx.strokeStyle=color;ctx.lineWidth=1.3;
      ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();ctx.setLineDash([]);
      ctx.fillStyle='rgba(11,14,20,.85)';
      const tw=ctx.measureText(label).width;
      ctx.fillRect(4,y-15,tw+10,14);
      ctx.fillStyle=color;ctx.font='bold 10px ui-monospace,Consolas,monospace';ctx.fillText(label,9,y-3);
    }
    if(pos){
      line(pos.entry,'#3b82f6','ENTRY '+smart(pos.entry));
      if(pos.sl)line(pos.sl,'#ea3943','SL '+smart(pos.sl));
      if(pos.tp)line(pos.tp,'#16c784','TP '+smart(pos.tp));
      document.getElementById('chartPos').textContent=pos.side.toUpperCase()+' · 入场 '+smart(pos.entry)+' · SL '+smart(pos.sl)+' · TP '+smart(pos.tp);
    }else{
      document.getElementById('chartPos').textContent='无持仓 · 最新 '+smart(last);
    }

    // 入场/出场圆点
    const xForTs=ts=>{
      let idx=cs.length-1;
      for(let i=0;i<cs.length;i++){ if(cs[i][0]>=ts){ idx=i; break; } }
      return idx*cw+cw/2;
    };
    const ts0=cs[0][0], ts1=cs[cs.length-1][0];
    const gap=cs.length>1?(ts1-ts0)/(cs.length-1):60000;
    const inRange=ts=>ts>=ts0-gap&&ts<=ts1+gap*3;
    const dot=(x,y,color,label)=>{
      ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);
      ctx.fillStyle=color;ctx.fill();
      ctx.strokeStyle='#0b0e14';ctx.lineWidth=1.5;ctx.stroke();
      if(label){ctx.fillStyle=color;ctx.font='bold 10px ui-monospace,Consolas,monospace';ctx.fillText(label,x+6,y+3);}
    };
    if(pos&&pos.opened_at&&inRange(pos.opened_at))dot(xForTs(pos.opened_at),Y(pos.entry),'#3b82f6','入场');
    (d.trades||[]).forEach(t=>{
      if(t.opened_at&&inRange(t.opened_at))dot(xForTs(t.opened_at),Y(t.entry),t.side==='long'?'#16c784':'#ea3943');
      if(t.closed_at&&inRange(t.closed_at))dot(xForTs(t.closed_at),Y(t.exit),'#7d8ba0');
    });

    // 十字光标
    const tip=document.getElementById('chartTip');
    cv.onmousemove=ev=>{
      const rect=cv.getBoundingClientRect();
      const mx=ev.clientX-rect.left;
      const idx=Math.min(cs.length-1,Math.max(0,Math.floor(mx/cw)));
      const c=cs[idx];
      const x=idx*cw+cw/2;
      // 重绘十字线
      drawChart(sym,tf);  // 简单方案：重绘后叠加
      ctx.setLineDash([2,2]);ctx.strokeStyle='rgba(255,255,255,.25)';ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke();
      const py=Y(c[4]);
      ctx.beginPath();ctx.moveTo(0,py);ctx.lineTo(w,py);ctx.stroke();ctx.setLineDash([]);
      tip.style.display='block';
      tip.style.left=(x+12)+'px';tip.style.top=(py+8)+'px';
      const d=new Date(c[0]);
      tip.innerHTML='<b>'+smart(c[4])+'</b><br>开 '+smart(c[1])+' · 高 '+smart(c[2])+' · 低 '+smart(c[3])+' · 收 '+smart(c[4])+'<br>量 '+volFmt(c[5])+' · '+d.toLocaleTimeString();
      if(tip.offsetLeft+tip.offsetWidth>rect.width)tip.style.left=(x-150)+'px';
    };
    cv.onmouseleave=()=>{tip.style.display='none';};
  });
}

/* ===== 权益曲线 ===== */
function drawEquity(curve,peak){
  const cv=document.getElementById('equityChart');
  if(!cv)return;
  const ctx=cv.getContext('2d');
  const w=cv.clientWidth, h=cv.clientHeight||120;
  const dpr=window.devicePixelRatio||1;
  cv.width=w*dpr; cv.height=h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  const data=(curve||[]).slice(-300);
  if(data.length<2){ctx.fillStyle='#7d8ba0';ctx.font='12px sans-serif';ctx.fillText('权益曲线等待数据…',12,20);return;}
  let lo=Infinity,hi=-Infinity;
  for(const p of data){lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1]);}
  if(peak)hi=Math.max(hi,peak);
  if(hi===lo){hi=lo+1;lo=lo-1;}
  const pad=(hi-lo)*0.12; lo-=pad; hi+=pad;
  const X=i=>i/(data.length-1)*w;
  const Y=v=>h-((v-lo)/(hi-lo))*h;

  // 渐变填充
  const grad=ctx.createLinearGradient(0,0,0,h);
  grad.addColorStop(0,'rgba(59,130,246,.28)');grad.addColorStop(1,'rgba(59,130,246,0)');
  ctx.beginPath();
  data.forEach((p,i)=>{const x=X(i),y=Y(p[1]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});
  const lastY=Y(data[data.length-1][1]);
  ctx.lineTo(w,lastY);ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();
  ctx.fillStyle=grad;ctx.fill();

  // 曲线
  ctx.beginPath();
  data.forEach((p,i)=>{const x=X(i),y=Y(p[1]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});
  ctx.strokeStyle='#3b82f6';ctx.lineWidth=1.6;ctx.stroke();

  // 峰值虚线
  if(peak){
    ctx.setLineDash([3,3]);ctx.strokeStyle='#f0b90b';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(0,Y(peak));ctx.lineTo(w,Y(peak));ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='#f0b90b';ctx.font='10px ui-monospace,Consolas,monospace';ctx.fillText('peak '+fmt(peak),4,Y(peak)-3);
  }
  ctx.fillStyle='#3b82f6';ctx.font='bold 10px ui-monospace,Consolas,monospace';
  ctx.fillText(''+fmt(data[data.length-1][1]),w-70,Y(data[data.length-1][1])-5);
}

/* ===== 持仓卡片 ===== */
function renderPosCards(list){
  const box=document.getElementById('posCards');
  document.getElementById('posCardCnt').textContent=list.length?('('+list.length+')'):'';
  if(!list.length){box.innerHTML='<div class="empty">无持仓</div>';return;}
  box.innerHTML=list.map(p=>{
    // SL→TP 进度条：把 entry 映射到 0-100
    let bar='', markerPct=50;
    if(p.sl&&p.tp&&p.price){
      const span=p.tp-p.sl;
      if(span>0){
        const curPct=Math.max(0,Math.min(100,(p.price-p.sl)/span*100));
        const entryPct=Math.max(0,Math.min(100,(p.entry-p.sl)/span*100));
        const long=p.side==='long';
        markerPct=entryPct;
        const from=Math.min(curPct,entryPct), wdt=Math.abs(curPct-entryPct);
        bar=`<div class="pos-bar"><div class="fill" style="left:${from}%;width:${wdt}%;background:${long?'rgba(14,203,129,.55)':'rgba(246,70,93,.55)'}"></div><div class="marker" style="left:${entryPct}%"></div></div>
        <div class="pos-meta"><span class="short">SL ${smart(p.sl)}</span><span>TP ${smart(p.tp)}</span></div>`;
      }
    }
    const dist=(p.dist_sl_pct!=null?'距SL '+p.dist_sl_pct+'% · ':'')+(p.dist_tp_pct!=null?'距TP '+p.dist_tp_pct+'%':'');
    return `<div class="pos-card ${p.side}">
      <div class="pos-head">
        <span class="sym">${esc(coin(p.symbol))}</span>
        <span class="side-tag ${p.side}">${p.side==='long'?'多':'空'}</span>
        <span class="lev">${p.leverage}x</span>
        <span class="sp"></span>
        <span class="pos-pnl ${pnlCls(p.pnl)}">${p.pnl>=0?'+':''}${fmt(p.pnl)}</span>
      </div>
      <div class="pos-row"><span>入场 <b>${smart(p.entry)}</b></span><span>现价 <b>${smart(p.price)}</b></span></div>
      <div class="pos-row"><span>ROE <b class="${pnlCls(p.pnl)}">${p.roe>=0?'+':''}${fmt(p.roe)}%</b></span><span>保证金 <b>${fmt(p.size_usd)}U</b> · 名义 <b>${fmt(p.notional)}U</b></span></div>
      ${bar}
      <div class="pos-meta" style="margin-top:3px"><span class="held">⏱ ${heldFmt(p.held_s)}</span><span>${dist}</span><button class="danger small" onclick="closePos('${esc(p.symbol)}')">平仓</button></div>
    </div>`;
  }).join('');
}

/* ===== 主刷新 ===== */
async function refresh(){
  try{
    const r=await fetch('/api/state'); const s=await r.json();
    const st=document.getElementById('status');
    st.className='badge '+(s.risk.killed?'kill':(s.paused?'pause':'on'));
    st.innerHTML='<span class="dot"></span>'+(s.risk.killed?'已熔断':(s.paused?'已暂停':'运行中'));
    const ws=document.getElementById('wsBadge');
    ws.className='badge '+(s.ws_healthy?'ws':'wsoff');
    ws.innerHTML='<span class="dot"></span>'+(s.ws_healthy?'WS 实时':'WS 离线');
    document.getElementById('uptime').textContent=s.uptime_s+'s';
    document.getElementById('clock').textContent=new Date(s.server_ts*1000).toLocaleTimeString();

    setVal('equity',fmt(s.risk.equity),'');
    document.getElementById('equitySub').textContent=(s.live?'真实余额':'模拟盘')+' · '+(s.signal_source==='rule'?'规则信号':'AI');
    setVal('daily',fmt(s.risk.daily_pnl),pnlCls(s.risk.daily_pnl));
    setVal('netpnl',fmt(s.stats.net_pnl),pnlCls(s.stats.net_pnl));
    setVal('unrealized',fmt(s.summary.unrealized),pnlCls(s.summary.unrealized));
    setVal('posnum',s.summary.open_count+' / '+s.stats.trades,'');
    document.getElementById('posSub').textContent='多 '+s.summary.long_n+' · 空 '+s.summary.short_n;
    setVal('winrate',fmt(s.stats.win_rate)+'%','');
    setVal('pf',fmt(s.stats.profit_factor),'');
    setVal('dd',fmt(s.stats.max_drawdown_pct)+'%',pnlCls(s.stats.max_drawdown_pct));

    // 榜单标题
    document.getElementById('boardTitle').textContent=(s.pool_type==='gainers'?'涨幅榜':'动量榜')+' · '+({long:'只做多',short:'只做空',both:'多空都做'}[s.direction]||s.direction);

    // 符号选择器
    const syms=[...new Set([...s.watchlist.map(w=>w.symbol),...s.positions.map(p=>p.symbol),...s.board.map(b=>b.symbol)])];
    if(!curSym&&syms.length)curSym=syms[0];
    const sel=document.getElementById('symSel');
    sel.innerHTML=syms.map(x=>`<option value="${esc(x)}" ${x===curSym?'selected':''}>${esc(coin(x))}</option>`).join('');

    // 榜单
    const bd=document.getElementById('board');
    if(s.board.length){
      bd.innerHTML=s.board.map((g,i)=>{
        const r=g.rets||{};
        const rc=(v,base)=>v==null?'<span class="ret-cell">--</span>':`<span class="${v>=0?'ret-up':'ret-down'}">${v>=0?'+':''}${fmt(v,2)}%</span>`;
        const has=g.in_positions?'<span class="'+(g.pos_side==='long'?'long':'short')+'" style="font-size:9px">●持仓</span>':'';
        const barW=Math.min(100,Math.abs(g.change_pct||0));
        return `<tr>
          <td class="dim">${i+1}</td>
          <td><span class="board-sym">${esc(coin(g.symbol))}</span>${has}<br><span class="board-coin mono">${esc(g.symbol)}</span></td>
          <td class="${(g.change_pct||0)>=0?'long':'short'}" style="font-weight:700">${(g.change_pct||0)>=0?'+':''}${fmt(g.change_pct)}%</td>
          <td>${rc(r['5m'])}</td><td>${rc(r['15m'])}</td><td>${rc(r['30m'])}</td><td>${rc(r['1h'])}</td><td>${rc(r['4h'])}</td>
          <td class="dim">${volFmt(g.quote_volume)}</td>
          <td class="mono">${smart(g.price)}</td>
        </tr>`;
      }).join('');
    }else{
      bd.innerHTML='<tr><td colspan="10" class="dim">榜单等待刷新（每 5 分钟）…</td></tr>';
    }

    // 持仓卡片
    renderPosCards(s.positions);

    // 监控列表
    const wl=document.getElementById('watchlist');
    wl.innerHTML=s.watchlist.length?s.watchlist.map(w=>`<tr><td class="mono">${esc(w.symbol)}</td><td class="${w.side==='buy'?'long':'short'}">${w.side}</td><td class="dim">${esc(w.summary||'')}</td><td><button class="ghost small" onclick="rmSym('${esc(w.symbol)}')">移除</button></td></tr>`).join(''):'<tr><td colspan="4" class="dim">等待信号…</td></tr>';

    // 成交
    const tr=document.getElementById('trades');
    tr.innerHTML=s.trades.length?s.trades.map(t=>`<tr><td class="mono">${esc(t.symbol)}</td><td class="${t.side==='long'?'long':'short'}">${t.side}</td><td class="mono">${t.leverage}x</td><td class="mono">${smart(t.entry)}</td><td class="mono">${smart(t.exit)}</td><td class="${pnlCls(t.pnl)}">${t.pnl>=0?'+':''}${fmt(t.pnl)}</td><td class="dim">${esc(t.reason)}</td></tr>`).join(''):'<tr><td colspan="7" class="dim">暂无成交</td></tr>';

    const fl=document.getElementById('fills');
    fl.innerHTML=(s.fills&&s.fills.length)?s.fills.map(f=>`<tr><td class="mono">${esc(f.order_id)}</td><td class="mono">${esc(f.symbol)}</td><td class="${f.side==='buy'?'long':'short'}">${f.side}</td><td class="mono">${smart(f.price)}</td><td class="mono">${fmt(f.qty)}</td></tr>`).join(''):'<tr><td colspan="5" class="dim">暂无撮合成交</td></tr>';

    const od=document.getElementById('orders');
    od.innerHTML=(s.orders&&s.orders.length)?s.orders.map(o=>`<tr><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc(o.symbol)}</td><td class="${o.side==='buy'?'long':'short'}">${o.side}</td><td class="mono">${o.type}</td><td>${fmt(o.notional)}</td><td class="${o.status==='FILLED'?'long':(o.status==='REJECTED'?'short':'')}">${o.status}</td><td class="mono">${smart(o.avg_price)}</td></tr>`).join(''):'<tr><td colspan="7" class="dim">暂无订单</td></tr>';

    const dc=document.getElementById('decisions');
    dc.innerHTML=(s.decisions&&s.decisions.length)?s.decisions.map(d=>`<tr><td class="mono">${esc(d.symbol)}</td><td class="${d.action==='LONG'?'long':(d.action==='SHORT'?'short':'')}">${d.action||d.side||'-'}</td><td>${fmt(d.confidence,0)}</td><td>${d.risk_level||'-'}</td><td class="dim">${esc(d.bull||'')}</td><td class="dim">${esc(d.bear||'')}</td><td class="dim">${esc(d.reason||'')}</td></tr>`).join(''):'<tr><td colspan="7" class="dim">暂无决策</td></tr>';

    const setC=(id,n)=>{const el=document.getElementById(id);if(el)el.textContent=n?('('+n+')'):'';};
    setC('tradeTabCnt',s.trades.length); setC('wlTabCnt',s.watchlist.length);

    document.getElementById('eqPeak').textContent=s.risk.peak_equity?('峰值 '+fmt(s.risk.peak_equity)):'';
    drawEquity(s.equity_curve,s.risk.peak_equity);
    drawChart(curSym,curTf);
  }catch(e){
    document.getElementById('status').textContent='连接失败';
  }
}

function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===name));
  document.querySelectorAll('.tabpage').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));
}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>switchTab(t.dataset.tab)));
function setVal(id,v,c){const el=document.getElementById(id);el.textContent=v;el.className='v '+c;}
function onSymChange(){curSym=document.getElementById('symSel').value;drawChart(curSym,curTf);}
function onTfChange(){curTf=document.getElementById('tfSel').value;drawChart(curSym,curTf);}
async function act(a){await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});refresh();}
async function addSym(){const s=document.getElementById('sym').value,side=document.getElementById('side').value;await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'add_symbol',symbol:s,side:side})});document.getElementById('sym').value='';refresh();}
async function rmSym(s){await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove_symbol',symbol:s})});refresh();}
async function closePos(s){if(!confirm('确认平仓 '+s+' ？'))return;await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'close_position',symbol:s})});refresh();}
refresh();setInterval(refresh,3000);
</script></body></html>
"""
