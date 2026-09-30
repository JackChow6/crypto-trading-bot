#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v43/tg_source.py —— TG 信号源

监听 Telegram 群，解析信号，把「交易对 + 方向」喂给引擎的 watchlist。
解析/进群逻辑在 tg_parse.py。
"""
from __future__ import annotations

from telethon import TelegramClient, events

import tg_parse


def _build_proxy(tc: dict):
    pcfg = tc.get("proxy") or {}
    if not pcfg.get("host"):
        return None
    import socks
    ptype = getattr(socks, str(pcfg.get("type", "socks5")).upper(), socks.SOCKS5)
    return (ptype, pcfg["host"], int(pcfg.get("port", 7897)), True)


async def run(cfg: dict, on_signal):
    """
    on_signal: async 回调，接收解析后的 Signal 对象。阻塞运行，直到被取消。
    """
    tc = cfg["telegram"]
    client = TelegramClient(tc["session_file"], tc["api_id"], tc["api_hash"],
                            proxy=_build_proxy(tc))
    await client.start()

    await tg_parse.maybe_join(client, tc.get("invite_link", ""))
    groups_cfg = tc.get("groups") or []
    chats = await tg_parse.resolve_chats(client, groups_cfg)
    if not chats:
        print("[tg_source] 未匹配到任何目标群")
        return
    print(f"[tg_source] 监听 {len(chats)} 个群")

    seen_ids = set()   # 去重：Telethon 偶发重复投递同一消息

    @client.on(events.NewMessage(chats=chats))
    async def handler(event):
        try:
            msg = event.message
            if msg.id in seen_ids:
                return
            seen_ids.add(msg.id)
            if len(seen_ids) > 20000:
                seen_ids.clear()
            text = (msg.text or msg.caption or "").strip()
            sig = tg_parse.parse_signal(text)
            if sig:
                print(f"[tg_source] 信号: {sig.summary()}")
                await on_signal(sig)
        except Exception as e:
            print(f"[tg_source] 处理消息出错: {e}")

    await client.run_until_disconnected()
