#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/tg_parse.py —— TG 信号解析 + 进群/解析群 工具（从旧 signal_bot.py 抽取，供引擎复用）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("tg_parse")

# ---------------------------------------------------------------------------
# 信号模型
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    chat: str
    sender: str
    symbol: str            # 如 BTC/USDT
    side: str              # buy | sell
    entry_ref: float | None = None
    stop_loss: float | None = None
    take_profits: list = field(default_factory=list)
    leverage: float | None = None
    raw: str = ""

    def summary(self) -> str:
        return (f"[{self.side.upper()}] {self.symbol} "
                f"entry={self.entry_ref} sl={self.stop_loss} "
                f"tp={self.take_profits[:4]} lev={self.leverage}")

# ---------------------------------------------------------------------------
# 信号解析
# ---------------------------------------------------------------------------
_PAIR_RE = re.compile(r"([A-Z]{3,12})\s*/\s*(USDT|USDC|BUSD|BTC|ETH|FDUSD)\b")
_COIN_FIELD_RE = re.compile(r"(?:coin|symbol|币种|交易对|标的)\s*[:：]?\s*#?([A-Z0-9]{2,12})\b")
_HASH_PAIR_RE = re.compile(r"#([A-Z0-9]{2,12})(USDT|USDC|BUSD)\b")
_RAW_PAIR_RE = re.compile(r"\b([A-Z]{3,10})(USDT|USDC|BUSD)\b")

_ENTRY_PATS = [r"\bentry\w*", r"入场", r"进场"]
_SL_PATS = [r"\bsl\b", r"stop\s*loss", r"\bstop\b", r"止损"]
_TP_PATS = [r"\btp\d*", r"take\s*profit\d*", r"止盈"]
_LEV_PATS = [r"\blev(?:erage)?\b", r"杠杆", r"\b\d+\s*x\b"]
_COIN_PATS = [r"\b(?:coin|symbol)\b", r"币种", r"交易对", r"标的"]
_SIDE_PATS = [r"\b(long|short|buy|sell)\b", r"做多", r"做空", r"买入", r"卖出"]

# 复盘/止盈达成/结果类消息：命中且无「新开仓字段」则跳过
_RESULT_PATS = [
    r"\bprofit\b", r"\bperiod\b", r"take[\s\-]*profit\s*target",
    r"\btp\s*\d*\s*(hit|reached|✅)", r"\bsl\s*hit\b", r"\bclosed\b",
    r"已止盈", r"止盈.{0,6}(达成|命中|触发|✅)", r"止损.{0,6}(触发|命中|达成)",
    r"盈利", r"复盘",
]
_FRESH_PATS = [r"\bentry\b", r"入场", r"进场", r"leverage", r"杠杆",
               r"signal\s*type", r"\bcross\b", r"\bisolated\b"]


def _is_result_recap(text: str) -> bool:
    has_result = any(re.search(p, text, re.I) for p in _RESULT_PATS)
    if not has_result:
        return False
    has_fresh = any(re.search(p, text, re.I) for p in _FRESH_PATS)
    return not has_fresh


def _field_numbers(text: str, want: str) -> list[float]:
    """块级字段提取：以字段标题出现位置为界切块。"""
    headers: list[tuple[int, str, int]] = []
    for key, pats in (("entry", _ENTRY_PATS), ("sl", _SL_PATS),
                      ("tp", _TP_PATS), ("lev", _LEV_PATS),
                      ("coin", _COIN_PATS), ("side", _SIDE_PATS)):
        for pat in pats:
            for m in re.finditer(pat, text, re.I):
                headers.append((m.end(), key, m.start()))
    headers.sort()

    out: list[float] = []
    for i, (hend, key, hstart) in enumerate(headers):
        if key != want:
            continue
        end = headers[i + 1][2] if i + 1 < len(headers) else len(text)
        toks = re.findall(r"\d+(?:\.\d+)?", text[hend:end])
        if not toks:
            continue
        if any("." in t for t in toks):
            vals = [float(t) for t in toks if "." in t]
        else:
            vals = [float(t) for t in toks]
        for v in vals:
            if v > 0 and v not in out:
                out.append(v)
    return out


def _first_match_pos(text: str, patterns) -> int | None:
    best = None
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def extract_symbol(text: str) -> str | None:
    for m in (_PAIR_RE.search(text), _COIN_FIELD_RE.search(text),
              _HASH_PAIR_RE.search(text), _RAW_PAIR_RE.search(text)):
        if m:
            g = m.groups()
            if len(g) == 2:
                return f"{g[0]}/{g[1]}"
            return f"{g[0]}/USDT"
    return None


def extract_side(text: str) -> str | None:
    b = _first_match_pos(text, [r"\b(buy|long)\b", r"做多", r"买入"])
    s = _first_match_pos(text, [r"\b(sell|short)\b", r"做空", r"卖出"])
    if b is None and s is None:
        return None
    if s is None or (b is not None and b < s):
        return "buy"
    return "sell"


def parse_signal(text: str, chat: str = "", sender: str = "") -> Signal | None:
    if not text:
        return None
    if _is_result_recap(text):
        log.debug("识别为复盘/结果消息，跳过: %s", text.replace("\n", " ")[:80])
        return None
    symbol = extract_symbol(text)
    side = extract_side(text)
    if not symbol or not side:
        return None

    entries = _field_numbers(text, "entry")
    sls = _field_numbers(text, "sl")
    tps = _field_numbers(text, "tp")
    levs = _field_numbers(text, "lev")
    if not levs:
        levs = [float(g) for g in re.findall(r"(\d+(?:\.\d+)?)\s*x", text, re.I)]

    return Signal(
        chat=chat, sender=sender, symbol=symbol, side=side,
        entry_ref=entries[0] if entries else None,
        stop_loss=sls[0] if sls else None,
        take_profits=tps[:6],
        leverage=levs[0] if levs else None,
        raw=text,
    )


# ---------------------------------------------------------------------------
# TG 群进群 / 解析
# ---------------------------------------------------------------------------
async def maybe_join(client, invite_link: str):
    """用已登录账号尝试加入邀请链接对应的私密群（已加入则忽略）。"""
    if not invite_link:
        return None
    link = invite_link.split("?")[0]
    hash_part = None
    if "+" in link:
        hash_part = link.rsplit("+", 1)[1]
    elif "joinchat/" in link:
        hash_part = link.rsplit("joinchat/", 1)[1]
    if not hash_part:
        log.warning("无法从邀请链接解析 hash: %s", invite_link)
        return None
    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        updates = await client(ImportChatInviteRequest(hash=hash_part))
        chats = getattr(updates, "chats", [])
        if chats:
            entity = await client.get_entity(chats[0])
            log.info("已加入群: %s (id=%s)", getattr(entity, "title", entity.id), entity.id)
            return entity
    except Exception as e:
        log.info("加入尝试未产生新群(可能已是成员/需审核): %s", e)
    return None


async def resolve_chats(client, entries):
    """groups 配置 -> 聊天实体：数字id/@用户名 直接解析，否则按标题模糊匹配。"""
    chats: list = []
    for raw in entries:
        s = str(raw).strip()
        resolved = None
        if s:
            try:
                resolved = await client.get_entity(int(s))
            except Exception:
                pass
            if resolved is None:
                try:
                    resolved = await client.get_entity(s.lstrip("@"))
                except Exception:
                    pass
        if resolved is not None:
            chats.append(resolved)
            log.info("已解析群: %s -> id=%s", s, resolved.id)
            continue
        log.info("直接解析失败，按标题匹配: %s", s)
        async for d in client.iter_dialogs():
            if d.name and s.lower() in d.name.lower():
                chats.append(d.entity)
                log.info("标题匹配: %s -> %s (id=%s)", s, d.name, d.id)
    seen, out = set(), []
    for c in chats:
        cid = getattr(c, "id", None)
        if cid in seen:
            continue
        seen.add(cid)
        out.append(c)
    return out
