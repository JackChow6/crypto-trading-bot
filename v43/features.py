#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/features.py —— Quant 特征引擎

把原始行情（OHLCV / 成交 / 盘口 / 持仓 / 资金费率）压成「低噪声、结构化」的市场快照。
纯 Python，无 numpy 依赖。全部输出可 JSON 序列化。
"""
from __future__ import annotations

import math
import time


# ---------------------------------------------------------------------------
# 基础指标
# ---------------------------------------------------------------------------
def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def atr(ohlcv: list, period: int = 14) -> float | None:
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    v = sum(trs[:period]) / period
    for t in trs[period:]:
        v = (v * (period - 1) + t) / period
    return v


def supertrend(ohlcv: list, period: int = 10, mult: float = 3.0):
    """Supertrend：返回 (趋势方向, 止损价, 上轨, 下轨)。

    趋势方向：1=多头（止损看下轨）、-1=空头（止损看上轨）。
    止损价 = 当前趋势对应的那根轨（做多取下轨、做空取上轨），价格反向突破即止损。
    数据不足返回 (0, None, None, None)。
    """
    if len(ohlcv) < period + 1:
        return 0, None, None, None
    highs = [b[2] for b in ohlcv]
    lows = [b[3] for b in ohlcv]
    closes = [b[4] for b in ohlcv]
    # ATR(period)
    trs = []
    for i in range(1, len(ohlcv)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr_val = sum(trs[:period]) / period
    atr_list = [None] * period
    atr_list.append(atr_val)
    for t in trs[period:]:
        atr_val = (atr_val * (period - 1) + t) / period
        atr_list.append(atr_val)
    # 逐根计算上下轨 + 趋势方向
    final_upper = [None] * len(ohlcv)
    final_lower = [None] * len(ohlcv)
    direction = [1] * len(ohlcv)   # 1=多, -1=空
    st_line = [None] * len(ohlcv)
    for i in range(period, len(ohlcv)):
        mid = (highs[i] + lows[i]) / 2
        base_upper = mid + mult * (atr_list[i] or 0.0)
        base_lower = mid - mult * (atr_list[i] or 0.0)
        # 最终轨：延续上一根趋势，避免轨线跳空
        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        if prev_upper is None:
            prev_upper, prev_lower = base_upper, base_lower
        final_upper[i] = base_upper if (base_upper < prev_upper or closes[i - 1] > prev_upper) else prev_upper
        final_lower[i] = base_lower if (base_lower > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        # 方向翻转（用上一根已确定的轨线判断，None 时用当前 base 轨兜底）
        ref_upper = final_upper[i - 1] if final_upper[i - 1] is not None else base_upper
        ref_lower = final_lower[i - 1] if final_lower[i - 1] is not None else base_lower
        if closes[i] > ref_upper:
            direction[i] = 1
        elif closes[i] < ref_lower:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
            # 延续方向时，轨线朝有利方向单向移动（上一根轨线已确定时才比较）
            if direction[i] == 1 and final_lower[i - 1] is not None and final_lower[i] < final_lower[i - 1]:
                final_lower[i] = final_lower[i - 1]
            if direction[i] == -1 and final_upper[i - 1] is not None and final_upper[i] > final_upper[i - 1]:
                final_upper[i] = final_upper[i - 1]
        # Supertrend 止损价
        st_line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
    return direction[-1], st_line[-1], final_upper[-1], final_lower[-1]


def _ema_series(data: list, period: int) -> list:
    k = 2 / (period + 1)
    out = [data[0]]
    for v in data[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD：返回 (macd线, 信号线, 柱状值) 的最后值。"""
    if len(closes) < slow + signal:
        return None, None, None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ef, es)]
    sig = _ema_series(macd_line, signal)
    return macd_line[-1], sig[-1], macd_line[-1] - sig[-1]


def bollinger(closes: list, period: int = 20, mult: float = 2.0):
    """布林带：返回 (上轨, 中轨, 下轨, %B, 带宽) 的最后值。"""
    if len(closes) < period:
        return None, None, None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    std = var ** 0.5
    upper = mid + mult * std
    lower = mid - mult * std
    pb = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5
    bandwidth = (upper - lower) / mid if mid else 0.0
    return upper, mid, lower, pb, bandwidth


def _round(x, n=8):
    return round(x, n) if x is not None else None


# ---------------------------------------------------------------------------
# 市场结构 / 支撑阻力
# ---------------------------------------------------------------------------
def swing_points(ohlcv: list, lookback: int = 5) -> tuple[list, list]:
    """局部摆动高低点，用于支撑/阻力。"""
    highs, lows = [], []
    for i in range(lookback, len(ohlcv) - lookback):
        win_h = ohlcv[i - lookback:i + lookback + 1]
        if ohlcv[i][2] == max(w[2] for w in win_h):
            highs.append((i, ohlcv[i][2]))
        if ohlcv[i][3] == min(w[3] for w in win_h):
            lows.append((i, ohlcv[i][3]))
    return highs, lows


def support_resistance(ohlcv: list, lookback: int = 5) -> dict:
    highs, lows = swing_points(ohlcv, lookback)
    res = sorted([h for _, h in highs])[-3:] if highs else []
    sup = sorted([l for _, l in lows])[:3] if lows else []
    return {
        "supports": [round(x, 8) for x in sup],
        "resistances": [round(x, 8) for x in res],
    }


def market_structure(ohlcv: list, lookback: int = 5) -> str:
    """HH/HL=上升，LH/LL=下降，否则震荡。"""
    highs, lows = swing_points(ohlcv, lookback)
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            return "UPTREND"
        if lh and ll:
            return "DOWNTREND"
    return "RANGE"


def structure_analysis(ohlcv: list, lookback: int = 5) -> dict:
    """识别市场结构：摆动高低点 + HH/HL/LH/LL + BOS(突破结构) + CHoCH(结构转变)。"""
    empty = {"trend": "RANGE", "swing_highs": [], "swing_lows": [],
             "last_swing_high": None, "last_swing_low": None,
             "hh": False, "hl": False, "lh": False, "ll": False,
             "bos": "NONE", "choch": "NONE"}
    if not ohlcv or len(ohlcv) < lookback * 2 + 1:
        return empty
    highs, lows = swing_points(ohlcv, lookback)
    sh = [h for _, h in highs]
    sl = [l for _, l in lows]
    price = ohlcv[-1][4]
    last_sh = sh[-1] if sh else None
    last_sl = sl[-1] if sl else None

    hh = len(sh) >= 2 and sh[-1] > sh[-2]
    lh = len(sh) >= 2 and sh[-1] < sh[-2]
    hl = len(sl) >= 2 and sl[-1] > sl[-2]
    ll = len(sl) >= 2 and sl[-1] < sl[-2]

    if hh and hl:
        trend = "UPTREND"
    elif lh and ll:
        trend = "DOWNTREND"
    else:
        trend = "RANGE"

    # BOS = 顺势突破结构；CHoCH = 逆势破结构（趋势反转早期信号）
    bos = "NONE"
    choch = "NONE"
    if trend == "UPTREND":
        if last_sh is not None and price > last_sh:
            bos = "BULLISH"       # 突破前高，上涨延续
        if last_sl is not None and price < last_sl:
            choch = "BEARISH"     # 跌破最近更高低点，上涨结构破坏
    elif trend == "DOWNTREND":
        if last_sl is not None and price < last_sl:
            bos = "BEARISH"       # 跌破前低，下跌延续
        if last_sh is not None and price > last_sh:
            choch = "BULLISH"     # 突破最近更低高点，下跌结构破坏

    return {
        "trend": trend,
        "swing_highs": [round(x, 8) for x in sh[-5:]],
        "swing_lows": [round(x, 8) for x in sl[-5:]],
        "last_swing_high": round(last_sh, 8) if last_sh is not None else None,
        "last_swing_low": round(last_sl, 8) if last_sl is not None else None,
        "hh": hh, "hl": hl, "lh": lh, "ll": ll,
        "bos": bos, "choch": choch,
    }


def kline_structure(ohlcv: list) -> dict:
    """把最近 K 线图结构喂给模型：阴阳序列 + 常见 K 线形态 + 最后一根形态描述。"""
    if not ohlcv:
        return {"candle_seq": "", "patterns": [], "last_candle": "",
                "body_ratio": 0.0, "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0}
    # 最近 10 根阴阳序列
    candle_seq = "".join("阳" if c[4] >= c[1] else "阴" for c in ohlcv[-10:])

    def shape(c):
        o, h, l, cl = c[1], c[2], c[3], c[4]
        body = abs(cl - o)
        rng = (h - l) if h > l else (body or 1e-12)
        upper = h - max(o, cl)
        lower = min(o, cl) - l
        return o, cl, body / rng, upper / rng, lower / rng

    patterns = []
    n = len(ohlcv)
    cur = ohlcv[-1]
    co, cc, body_ratio, upper_ratio, lower_ratio = shape(cur)
    # 十字星
    if body_ratio < 0.1:
        patterns.append("DOJI")
    # 锤子 / 上吊线（长下影、小实体、实体在上半部）
    if lower_ratio > 0.6 and body_ratio < 0.4:
        mid = (co + cc) / 2
        rng_mid = (cur[2] + cur[3]) / 2
        if mid > rng_mid:
            patterns.append("HAMMER" if cc >= co else "HANGING_MAN")
    # 流星（长上影、小实体、实体在下半部）
    if upper_ratio > 0.6 and body_ratio < 0.4:
        mid = (co + cc) / 2
        rng_mid = (cur[2] + cur[3]) / 2
        if mid < rng_mid:
            patterns.append("SHOOTING_STAR")
    # 吞没形态（当前根吞没上一根相反方向）
    if n >= 2:
        po, pc = shape(ohlcv[-2])[0], ohlcv[-2][4]
        prev_bull = pc >= po
        cur_bull = cc >= co
        if cur_bull and not prev_bull and cc > po and co < pc:
            patterns.append("BULLISH_ENGULFING")
        elif not cur_bull and prev_bull and cc < po and co > pc:
            patterns.append("BEARISH_ENGULFING")
    # 三连阳 / 三连阴
    if n >= 3:
        if all(ohlcv[n - 1 - i][4] >= ohlcv[n - 1 - i][1] for i in range(3)):
            patterns.append("THREE_WHITE_SOLDIERS")
        if all(ohlcv[n - 1 - i][4] < ohlcv[n - 1 - i][1] for i in range(3)):
            patterns.append("THREE_BLACK_CROWS")

    # 最后一根形态描述
    bull = "阳" if cc >= co else "阴"
    size = "大" if body_ratio > 0.6 else ("小" if body_ratio < 0.25 else "中")
    wick = ""
    if upper_ratio > 0.4:
        wick += "长上影"
    if lower_ratio > 0.4:
        wick += "长下影"
    last_candle = f"{size}{bull}线" + (f"({wick})" if wick else "")
    return {
        "candle_seq": candle_seq,
        "patterns": patterns,
        "last_candle": last_candle,
        "body_ratio": round(body_ratio, 3),
        "upper_wick_ratio": round(upper_ratio, 3),
        "lower_wick_ratio": round(lower_ratio, 3),
    }


# ---------------------------------------------------------------------------
# 订单流 / 衍生品
# ---------------------------------------------------------------------------
def orderbook_imbalance(bids: list, asks: list, depth: int = 10) -> float:
    """买卖盘深度失衡：>0 买盘占优，<0 卖盘占优。"""
    bv = sum(q for _, q in bids[:depth])
    av = sum(q for _, q in asks[:depth])
    if bv + av == 0:
        return 0.0
    return (bv - av) / (bv + av)


def _trade_fields(t):
    """兼容 ccxt 成交的 list / dict 两种格式，返回 (ts_ms, price, amount, side)。"""
    if isinstance(t, dict):
        return (t.get("timestamp") or 0, t.get("price") or 0,
                t.get("amount") or 0, t.get("side"))
    try:
        # ccxt list: [id, ts, datetime, symbol, order, type, side, takerOrMaker, price, amount, cost, ...]
        return (t[1] or 0, t[8] or 0, t[9] or 0, (t[6] if len(t) > 6 else None))
    except (IndexError, TypeError):
        return (0, 0, 0, None)


def delta_cvd(trades: list) -> dict:
    """
    由成交流计算订单流特征：
      - Delta（1m/5m 主动买卖净额）与 CVD（累计）+ CVD 趋势
      - 主动买入占比 taker_buy_ratio（5m 窗口）
      - 主动买/卖量 buy_vol_5m / sell_vol_5m
      - 大单数量 large_trades（> 2× 中位成交量，反映主力/鲸鱼动作）
    主动方向优先取成交 side 字段(buy/sell)；无 side 时按「成交价 vs 前价」近似推断。
    """
    base = {"delta_1m": 0.0, "delta_5m": 0.0, "cvd": 0.0, "cvd_direction": "FLAT",
            "cvd_trend": "FLAT", "taker_buy_ratio": 0.5,
            "buy_vol_5m": 0.0, "sell_vol_5m": 0.0, "large_trades": 0}
    if not trades:
        return base
    now_ms = time.time() * 1000
    prev_px = None
    delta_1m = delta_5m = cvd = 0.0
    buy_vol_5m = sell_vol_5m = 0.0
    amounts: list[float] = []
    cvd_seq: list[float] = []
    for t in trades:
        ts, px, qty, side = _trade_fields(t)
        try:
            px, qty = float(px), float(qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        amounts.append(qty)
        if side in ("buy", "sell"):
            s = 1 if side == "buy" else -1
        else:
            s = 1 if (prev_px is None or px >= prev_px) else -1
        cvd += s * qty
        cvd_seq.append(cvd)
        age_ms = now_ms - (ts or 0)
        if 0 <= age_ms <= 60_000:
            delta_1m += s * qty
        if 0 <= age_ms <= 300_000:
            delta_5m += s * qty
            if side == "buy":
                buy_vol_5m += qty
            elif side == "sell":
                sell_vol_5m += qty
        prev_px = px
    direction = "RISING" if cvd > 0 else ("FALLING" if cvd < 0 else "FLAT")
    # CVD 趋势：后半段均值 vs 前半段均值
    cvd_trend = "FLAT"
    if len(cvd_seq) >= 10:
        half = len(cvd_seq) // 2
        early = sum(cvd_seq[:half]) / half
        late = sum(cvd_seq[half:]) / (len(cvd_seq) - half)
        if late > early:
            cvd_trend = "RISING"
        elif late < early:
            cvd_trend = "FALLING"
    # 主动买入占比（5m 窗口）
    tv = buy_vol_5m + sell_vol_5m
    taker_buy_ratio = round(buy_vol_5m / tv, 4) if tv else 0.5
    # 大单数量：成交量 ≥ 2× 中位数
    large_trades = 0
    if amounts:
        med = sorted(amounts)[len(amounts) // 2]
        if med > 0:
            large_trades = sum(1 for a in amounts if a >= 2 * med)
    return {"delta_1m": round(delta_1m, 2), "delta_5m": round(delta_5m, 2),
            "cvd": round(cvd, 2), "cvd_direction": direction,
            "cvd_trend": cvd_trend, "taker_buy_ratio": taker_buy_ratio,
            "buy_vol_5m": round(buy_vol_5m, 2), "sell_vol_5m": round(sell_vol_5m, 2),
            "large_trades": large_trades}


# ---------------------------------------------------------------------------
# 汇总：多周期市场快照
# ---------------------------------------------------------------------------
def build_snapshot(md: dict) -> dict:
    """
    md 需包含:
      ohlcv: 1m K线 [[ts,o,h,l,c,v], ...]
      trades(可选), orderbook{bids,asks}(可选), oi(可选), funding(可选)
    输出结构化 Market Snapshot（供本地小模型 + 事件检测使用）。
    """
    ohlcv = md.get("ohlcv") or []
    if not ohlcv:
        return {}
    closes = [c[4] for c in ohlcv]
    price = md.get("last") or closes[-1]   # 优先用实时 ticker 价，回退 1m 收盘价
    atr14 = atr(ohlcv, 14) or 0.0
    atr_pct = (atr14 / price * 100) if price else 0.0
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    macd_line, macd_signal, macd_hist = macd(closes)
    bb_upper, bb_mid, bb_lower, bb_pb, bb_width = bollinger(closes)
    sr = support_resistance(ohlcv)
    res = sr["resistances"]
    sup = sr["supports"]
    # 回踩检测（做多）：上升趋势中，价格回落到 EMA20 或最近支撑附近（不追突破）
    ema20_last = ema20[-1] if ema20 else None
    ema50_last = ema50[-1] if ema50 else None
    pb_thr = float(md.get("pullback_thr", 0.02))   # 距 EMA/支撑 ≤2% 视为"回踩到"
    uptrend = bool(ema20_last and ema50_last and ema20_last >= ema50_last)
    near_ema = bool(ema20_last and price >= ema20_last and (price - ema20_last) / ema20_last <= pb_thr)
    near_sup = False
    pullback_level = None
    below_sup = [s for s in sup if s <= price]
    if below_sup:
        nearest = max(below_sup)
        if (price - nearest) / nearest <= pb_thr:
            near_sup = True
            pullback_level = nearest
    if near_ema:
        pullback_level = ema20_last
    pullback = bool(uptrend and (near_ema or near_sup))
    last_high = ohlcv[-1][2]
    last_low = ohlcv[-1][3]
    breakout = bool(res and last_high > res[-1])          # 突破最近阻力
    breakdown = bool(sup and last_low < sup[-1])          # 跌破最近支撑
    vol = [c[5] for c in ohlcv]
    vol_ratio = (sum(vol[-5:]) / (sum(vol[-20:-5]) / 15)) if len(vol) >= 20 and sum(vol[-20:-5]) else 1.0
    # 成交量特征：给 AI 判断量能配合（放量突破/缩量回踩等）
    vol_bars = [round(float(v), 2) for v in vol[-15:]]
    avg_vol = (sum(vol[-20:-1]) / 19) if len(vol) >= 20 else ((sum(vol) / len(vol)) if vol else 0)
    vol_spike = bool(vol and avg_vol and vol[-1] >= 2 * avg_vol)
    vol_trend = "FLAT"
    if len(vol) >= 15:
        recent_v = sum(vol[-5:]) / 5
        prior_v = sum(vol[-15:-5]) / 10
        if prior_v > 0 and recent_v >= prior_v * 1.2:
            vol_trend = "INCREASING"
        elif prior_v > 0 and recent_v <= prior_v * 0.8:
            vol_trend = "DECREASING"
    # 最近 12 根 1m K 线 [开,高,低,收,量]，让 AI 看真实价量动作
    recent_candles = [[round(c[1], 8), round(c[2], 8), round(c[3], 8), round(c[4], 8), round(c[5], 2)]
                      for c in ohlcv[-12:]]

    ob = md.get("orderbook") or {}
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    imbalance = orderbook_imbalance(bids, asks)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    flow = delta_cvd(md.get("trades") or [])

    # Supertrend（趋势方向 + 动态止损位）：确定性趋势跟随信号源的核心指标
    st_dir, st_line, st_upper, st_lower = supertrend(ohlcv)

    return {
        "symbol": md.get("symbol"),
        "price": _round(price),
        "atr": _round(atr14),
        "atr_pct": _round(atr_pct, 4),
        "ema20": _round(ema20[-1] if ema20 else None),
        "ema50": _round(ema50[-1] if ema50 else None),
        "rsi14": _round(rsi(closes, 14)),
        "macd": {"line": _round(macd_line), "signal": _round(macd_signal), "hist": _round(macd_hist)},
        "bollinger": {"upper": _round(bb_upper), "middle": _round(bb_mid), "lower": _round(bb_lower),
                      "percent_b": _round(bb_pb, 4), "bandwidth": _round(bb_width, 4)},
        "vol_ratio": _round(vol_ratio, 3),
        "volume": {"trend": vol_trend, "spike": vol_spike, "recent": vol_bars},
        "recent_candles": recent_candles,
        "kline_structure": kline_structure(ohlcv),
        "trend": {
            "short": "UP" if len(closes) > 5 and closes[-1] > closes[-6] else "DOWN",
            "mid": "UP" if len(closes) > 15 and closes[-1] > closes[-16] else "DOWN",
            "long": "UP" if len(closes) > 60 and closes[-1] > closes[-61] else "DOWN",
        },
        "structure": market_structure(ohlcv),
        "structure_detail": structure_analysis(ohlcv),
        "support": sup, "resistance": res,
        "breakout": breakout, "breakdown": breakdown,
        "pullback": pullback,
        "pullback_level": _round(pullback_level),
        "near_ema20": near_ema,
        "near_support": near_sup,
        "orderbook": {"imbalance": _round(imbalance, 4),
                      "best_bid": _round(best_bid), "best_ask": _round(best_ask)},
        "orderflow": flow,
        "supertrend": {"direction": st_dir, "line": _round(st_line),
                       "upper": _round(st_upper), "lower": _round(st_lower)},
        "derivatives": {
            "oi": _round(md.get("oi")),
            "oi_change_5m_pct": _round(md.get("oi_change_5m_pct"), 4),
            "funding": _round(md.get("funding"), 8),
        },
    }
