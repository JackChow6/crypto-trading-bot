#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/ai.py —— 本地大模型（Ollama Qwen3）单次直接决策

把完整市场快照（指标 + 量价 + 订单流 + K线形态 + 市场结构）一次性喂给模型，
直接输出 LONG/SHORT/NO_TRADE + 置信度 + 风险等级 + 多空理由 + 决策理由。

只做一次模型调用，不再分「观察员→机会门→辩论→交易员」多步。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import requests

# 串行化所有 Ollama 调用：本地模型一次只能跑一个，并发会 500/卡死
_ollama_lock = threading.Lock()


def call_ollama(cfg: dict, prompt: str) -> str:
    url = cfg.get("base_url", "http://127.0.0.1:11434/v1").rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.get("model", "qwen3:8b"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": int(cfg.get("num_ctx", 8192))},
        "response_format": {"type": "json_object"},
        "think": False,   # 关掉思考模式，让 qwen3 直接输出 JSON（否则内容空/500）
    }
    last_err = ""
    for _ in range(3):
        try:
            with _ollama_lock:
                r = requests.post(url, json=body, headers={"Authorization": f"Bearer {cfg.get('api_key', 'ollama')}"},
                                  timeout=int(cfg.get("timeout", 120)))
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content and content.strip():
                return content
            last_err = "模型返回空内容"
        except Exception as e:
            last_err = str(e)
        time.sleep(2)
    raise RuntimeError(f"Ollama 调用失败(重试3次): {last_err}")


def _parse_json(text: str) -> dict:
    t = re.sub(r"```(?:json)?", "", text).strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        raise ValueError("无 JSON")
    return json.loads(t[s:e + 1])


_DECISION_TEMPLATE = """你是加密货币合约交易员。请在同一轮里先做「多空辩论」，再给出最终决策。

【触发事件】{events}

【当前市场快照】
（含指标 EMA/RSI/MACD/布林、量价 volume、订单流 orderflow、K线形态 kline_structure、市场结构 structure_detail、支撑阻力等）
{snapshot}

【当前持仓】{positions}

【风险状态】{risk}

【跟单信号方向】{signal}

【最近交易复盘】{recent_trades}

步骤（在这一次输出里完成）:
1) 多空辩论：分别站在多头和空头视角给出理由，写入 bull 和 bear 字段。
2) 最终决策：综合多空理由，输出 LONG / SHORT / NO_TRADE + 置信度 + 风险等级 + 理由。

规则:
1) 只输出 LONG / SHORT / NO_TRADE 三者之一。
2) 信号方向明确时(LONG/SHORT)必须跟随该方向，不得反向开仓；仅当信号方向为"无"时才可自主判断方向。
3) 综合判断量价配合：volume(趋势/是否放量)、orderflow(taker_buy_ratio/delta/CVD趋势/大单large_trades)、kline_structure(阴阳序列/吞没/锤子/十字星/三连阳阴/最后一根形态)、structure_detail(摆动高低点/HH-HL-LH-LL/BOS顺势突破/CHoCH结构转变)。放量突破/跌破更可信、缩量回踩更健康，量价/订单流/结构背离要谨慎。
4) 参考【最近交易复盘】避免重复同样错误，但不要仅因"近期有亏损"就拒单。
5) 有止损才允许进场；不确定就 NO_TRADE。
6) risk_level 按把握大小给 1(低风险可重仓)~5(高风险轻仓或放弃)。
7) 不要输出具体价格，入场/止盈/止损由系统按 ATR 自动计算。
8) 严格输出 JSON，不要任何其它文字。

输出格式:
{{"action":"LONG|SHORT|NO_TRADE","confidence":0到100的整数,"risk_level":1到5,"bull":"看多理由","bear":"看空理由","reason":"最终决策理由"}}"""


@dataclass
class Decision:
    action: str = "NO_TRADE"
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    confidence: float = 0.0
    risk_level: int = 3
    bull: str = ""
    bear: str = ""
    reason: str = ""
    raw: str = ""


def _fmt_events(events) -> str:
    return json.dumps([e.__dict__ if hasattr(e, "__dict__") else e for e in events], ensure_ascii=False, default=str)


def build_decision_prompt(events, snapshot, positions, risk, signal=None, recent_trades=None) -> str:
    sig_text = json.dumps(signal, ensure_ascii=False) if signal else "无（方向自主判断）"
    trades_text = json.dumps(recent_trades, ensure_ascii=False) if recent_trades else "暂无（刚开始交易）"
    return _DECISION_TEMPLATE.format(
        events=_fmt_events(events),
        snapshot=json.dumps(snapshot, ensure_ascii=False),
        positions=json.dumps(positions, ensure_ascii=False),
        risk=json.dumps(risk, ensure_ascii=False),
        signal=sig_text,
        recent_trades=trades_text,
    )


def decision(cfg: dict, events, snapshot, positions, risk, signal=None, recent_trades=None) -> Decision:
    """单次直接决策：完整快照 → LONG/SHORT/NO_TRADE。"""
    try:
        txt = call_ollama(cfg, build_decision_prompt(events, snapshot, positions, risk, signal, recent_trades))
        obj = _parse_json(txt)
        action = str(obj.get("action", "NO_TRADE")).upper()
        if action not in ("LONG", "SHORT", "NO_TRADE"):
            action = "NO_TRADE"
        d = Decision(action=action, raw=txt)
        d.bull = str(obj.get("bull", ""))[:200]
        d.bear = str(obj.get("bear", ""))[:200]
        try:
            d.confidence = max(0.0, min(100.0, float(obj.get("confidence", 0))))
        except (TypeError, ValueError):
            d.confidence = 0.0
        try:
            d.risk_level = max(1, min(5, int(obj.get("risk_level", 3))))
        except (TypeError, ValueError):
            d.risk_level = 3
        d.reason = str(obj.get("reason", "")).strip()[:200]
        if not d.reason:
            d.reason = (d.bull or d.bear or "未给出理由")[:200]
        return d
    except Exception as e:
        return Decision(action="NO_TRADE", reason=f"决策模型调用失败: {e}")


_REVIEW_TEMPLATE = """你是加密货币持仓管理助手。根据当前持仓和最新市场快照，判断该持仓的止盈止损该如何调整。

【当前持仓】
{position}

【最新市场快照】
{snapshot}

任务：只输出一个动作：
- tighten_sl：持仓已有浮盈，收紧止损（把止损向当前价方向移动），锁定更多利润。
- raise_tp：趋势有利，上调止盈目标，让利润跑更远。
- cut_loss：持仓浮亏且趋势已转坏（跌破支撑/结构破坏/订单流转空），把止损向当前价方向收紧，提前离场控制亏损。
- hold：维持现状，继续持有。

规则:
1) 有浮盈时才建议 tighten_sl 或 raise_tp；趋势仍有利时 hold。
2) 浮亏时：若趋势明显转坏（short trend DOWN + 跌破支撑 + 订单流转空），输出 cut_loss 提前止损；否则 hold 等固定止损。
3) 结合快照里的趋势(trend/structure_detail)、量价(volume/orderflow)、K线形态(kline_structure) 判断是否延续。
4) 严格输出 JSON，不要任何其它文字。

输出格式:
{{"action":"tighten_sl|raise_tp|cut_loss|hold","reason":"一句中文"}}"""


def review_position(cfg: dict, pos_info: dict, snapshot: dict) -> dict:
    """AI 定期复查持仓：输出 tighten_sl / raise_tp / cut_loss / hold。"""
    try:
        prompt = _REVIEW_TEMPLATE.format(
            position=json.dumps(pos_info, ensure_ascii=False),
            snapshot=json.dumps(snapshot, ensure_ascii=False),
        )
        txt = call_ollama(cfg, prompt)
        obj = _parse_json(txt)
        action = str(obj.get("action", "hold")).lower()
        if action not in ("tighten_sl", "raise_tp", "cut_loss", "hold"):
            action = "hold"
        reason = str(obj.get("reason", "")).strip()[:200]
        if not reason:
            reason = {"tighten_sl": "AI判断收紧止损", "raise_tp": "AI判断上调止盈",
                      "cut_loss": "AI判断趋势转坏提前止损", "hold": "AI判断继续持有"}.get(action, "未给出理由")
        return {"action": action, "reason": reason}
    except Exception as e:
        return {"action": "hold", "reason": f"复查失败: {e}"}
