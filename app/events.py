#!/usr/bin/env python3
"""事件分发：监控发现新微博 → Telegram 快推 + insight webhook（分析联动）。

TG 配置与推送实现复用 settings 表里的 tg_bot_token/tg_chat_id
（与 radar 容器各自的 settings 独立，本容器首次使用需在页面配置）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .store import Store

log = logging.getLogger("weibo.events")

INSIGHT_API = os.environ.get("INSIGHT_API", "").rstrip("/")
WEBHOOK_ENABLED = os.environ.get("EVENT_WEBHOOK", "1") != "0"


def tg_config(store: Store) -> tuple:
    return (store.get_setting("tg_bot_token"),
            store.get_setting("tg_chat_id"))


def tg_send(store: Store, text: str) -> bool:
    token, chat = tg_config(store)
    if not token or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text,
                  "disable_web_page_preview": True},
            timeout=10)
        return r.status_code == 200
    except requests.RequestException as e:
        log.warning("tg send: %s", e)
        return False


def dispatch_new_weibos(store: Store, uid: str,
                        weibos: List[Dict]) -> None:
    """新微博事件分发：快推原文 + 转发 insight 分析。

    weibos: [{bid, created_at, text}, ...]（新的在前）
    """
    if not weibos:
        return
    # 博主昵称从库内 user 表读（不硬编码，仓库公开也不暴露监控对象）
    u = store.get_user() or {}
    name = u.get("screen_name") or uid
    for w in weibos[:3]:  # 单轮最多处理 3 条，防刷屏
        # (a) 快通道：原文秒推
        snippet = (w.get("text") or "")[:80].replace("\n", " ")
        tg_send(store,
                f"🔔 {name} 发新微博（{datetime.now():%H:%M}）\n"
                f"{snippet}…\n"
                f"分析中，决策卡片随后到 →")
        # (b) 分析通道：insight webhook
        if INSIGHT_API and WEBHOOK_ENABLED:
            try:
                requests.post(
                    f"{INSIGHT_API}/api/event/weibo",
                    json={"uid": uid, "bid": w["bid"],
                          "text": w.get("text") or ""},
                    timeout=15)
            except requests.RequestException as e:
                log.warning("insight webhook: %s", e)
