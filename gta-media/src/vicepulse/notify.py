"""Phone notifications through a Telegram bot (free). Off unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

Best effort: a failed notification never stops clipping, and the bot token is never logged or shown.
Setup: talk to @BotFather in Telegram -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN. Then send
any message to your new bot and run `vicepulse telegram-setup` to find your chat id.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

API = "https://api.telegram.org"


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _url(method: str) -> str:
    return f"{API}/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"


def send(text: str, video: Path | None = None, session: requests.Session | None = None) -> bool:
    if not enabled():
        return False
    http = session or requests.Session()
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        if video and video.is_file() and video.stat().st_size < 49 * 1024 * 1024:
            with open(video, "rb") as f:
                r = http.post(_url("sendVideo"), data={"chat_id": chat, "caption": text[:1000], "supports_streaming": "true"},
                              files={"video": (video.name, f, "video/mp4")}, timeout=120)
        else:
            r = http.post(_url("sendMessage"), data={"chat_id": chat, "text": text[:4000]}, timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return False


def find_chat_ids(session: requests.Session | None = None) -> list[dict]:
    """Chats that recently messaged the bot (to fill TELEGRAM_CHAT_ID). Needs only TELEGRAM_BOT_TOKEN."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise ValueError("set TELEGRAM_BOT_TOKEN in .env first (from @BotFather)")
    r = (session or requests.Session()).get(_url("getUpdates"), timeout=30)
    if r.status_code != 200:
        raise ValueError(f"Telegram refused the bot token ({r.status_code})")
    seen = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = {"chat_id": chat["id"], "name": chat.get("title") or chat.get("first_name") or chat.get("username")}
    return list(seen.values())
