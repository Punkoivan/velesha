"""Inbound Telegram: talk to Velesha from a chat, read-only for now (ADR-0070).

Long-polls getUpdates (no public webhook needed — acer is LAN-only). Only
private chats from TELEGRAM_ALLOWED_USERS ("<telegram user id>:<person>,…")
are answered; anyone else is ignored without a reply (and logged), so the
bot doesn't even confirm it's alive. The update offset survives restarts in
data/telegram_offset.json — a message is never answered twice.
"""

import asyncio
import json
import os
import pathlib
import re
from collections.abc import Awaitable, Callable

import requests

import telegram_client

_STATE = pathlib.Path(__file__).parent / "data" / "telegram_offset.json"
_POLL_TIMEOUT = 50
_MAX_TEXT = 4000  # Telegram's limit is 4096


def allowed() -> dict[str, str]:
    pairs = (p.split(":", 1) for p in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if ":" in p)
    return {uid.strip(): person.strip() for uid, person in pairs}


def family_chats() -> list[str]:
    """Group chats where the allowlisted people may also CONTROL the home (ADR-0077)."""
    return [c.strip() for c in os.environ.get("TELEGRAM_FAMILY_CHATS", "").split(",") if c.strip()]


_me: dict = {}


def _bot_username() -> str:
    if "username" not in _me:
        _me["username"] = _api("getMe").get("result", {}).get("username", "")
    return _me["username"]


def _offset() -> int:
    try:
        return int(json.loads(_STATE.read_text())["offset"])
    except (FileNotFoundError, ValueError, KeyError):
        return 0


def _save_offset(offset: int) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({"offset": offset}))


def _api(method: str, **params) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{telegram_client.TELEGRAM_BOT_TOKEN}/{method}",
                      json=params, timeout=_POLL_TIMEOUT + 15)
    r.raise_for_status()
    return r.json()


def _updates(offset: int) -> list[dict]:
    return _api("getUpdates", offset=offset, timeout=_POLL_TIMEOUT, allowed_updates=["message"]).get("result", [])


async def loop(handle: Callable[..., Awaitable[str]]) -> None:
    """handle(text, person, forwarded, group_chat_id) -> answer; run in its own task per message so its
    contextvars (current user, read-only flag) never leak into the next one."""
    if not telegram_client.available() or not allowed():
        print("telegram bot: off (no token or TELEGRAM_ALLOWED_USERS)", flush=True)
        return
    offset = _offset()
    if not _STATE.exists():  # first start: whatever piled up before the bot listened is not a question to answer now
        try:
            old = await asyncio.to_thread(_api, "getUpdates", offset=-1, timeout=0)
            offset = (old.get("result") or [{"update_id": -1}])[-1]["update_id"] + 1
            _save_offset(offset)
        except Exception as e:
            print(f"telegram backlog skip error: {type(e).__name__}", flush=True)
    while True:
        try:
            updates = await asyncio.to_thread(_updates, offset)
        except Exception as e:  # network blip — back off a little, keep the offset
            print(f"telegram poll error: {type(e).__name__}", flush=True)  # no URL: it carries the bot token
            await asyncio.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            _save_offset(offset)  # before answering: a crash mid-answer must not replay it forever
            msg = u.get("message") or {}
            text, chat, sender = msg.get("text"), msg.get("chat") or {}, msg.get("from") or {}
            person = allowed().get(str(sender.get("id")))
            in_family = chat.get("type") in ("group", "supergroup") and str(chat.get("id")) in family_chats()
            if not person or not (chat.get("type") == "private" or in_family):
                print(f"telegram: ignored message from {sender.get('id')} {sender.get('first_name', '')!r} in "
                      f"{chat.get('type')} chat {chat.get('id')} {chat.get('title', '')!r}", flush=True)  # ids for the allowlist
                continue
            if not text:
                await asyncio.to_thread(telegram_client.send_message, chat["id"], "Поки що розумію лише текст.")
                continue
            if in_family:  # privacy mode delivers only mentions/replies; drop the "@bot" itself
                bot = await asyncio.to_thread(_bot_username)
                text = re.sub(rf"@{re.escape(bot)}\b", "", text, flags=re.IGNORECASE).strip() if bot else text
            try:
                await asyncio.to_thread(_api, "sendChatAction", chat_id=chat["id"], action="typing")
                forwarded = bool(msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date"))
                answer = await asyncio.create_task(handle(text, person, forwarded, str(chat["id"]) if in_family else None))
            except Exception as e:
                print(f"telegram handle error: {type(e).__name__}: {e}", flush=True)
                answer = "Не вдалося відповісти — спробуй ще раз."
            try:
                await asyncio.to_thread(telegram_client.send_message, chat["id"], (answer or "…")[:_MAX_TEXT],
                                        msg.get("message_id") if in_family else None)
            except Exception as e:
                print(f"telegram send error: {type(e).__name__}", flush=True)
