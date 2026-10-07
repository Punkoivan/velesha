"""Minimal Telegram Bot API client — outbound notifications only (ADR-0054),
no incoming-command handling. Token in api/secrets.enc.env.
"""

import os

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
_API = "https://api.telegram.org/bot{token}/{method}"


def available() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def send_message(chat_id: str, text: str, reply_to: int | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set — run via sops exec-env secrets.enc.env")
    data = {"chat_id": chat_id, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to  # in a group: answer as a reply to the asker's message
    r = requests.post(_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"), data=data, timeout=15)
    r.raise_for_status()
