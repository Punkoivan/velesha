"""Telegram reminders for Grocy chores (ADR-0066) — Grocy tracks them but never pushes.

Once a day after _SEND_AT: a chore due within CHORE_NOTIFY_LEAD_DAYS gets one
"скоро" message (time to book the vet), and one more when it's due or
overdue. Sent flags are keyed by the due date in data/chore_notify.json, so
marking the chore done in Grocy (new due date) starts the cycle over.
"""

import asyncio
import datetime as dt
import json
import os
import pathlib
import zoneinfo

import grocy_client
import telegram_client

TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
_STATE = pathlib.Path(__file__).parent / "data" / "chore_notify.json"
_TICK = 600
_SEND_AT = dt.time(9, 0)


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(state, ensure_ascii=False))


def messages(chores: list[dict], today: dt.date, lead: int, state: dict) -> list[tuple[str, str, str]]:
    """(chore_id, kind, text) still to send; kind is "soon" or "due"."""
    out = []
    for c in chores:
        due_raw = c.get("next_estimated_execution_time")
        if not due_raw:
            continue  # manually scheduled chore with no due date
        due = dt.date.fromisoformat(due_raw[:10])
        cid = str(c["chore_id"])
        sent = state.get(cid, {}).get(due.isoformat(), [])
        days = (due - today).days
        if days <= 0 and "due" not in sent:
            when = "сьогодні" if days == 0 else f"прострочено на {-days} дн."
            out.append((cid, "due", f"Нагадування: {c['chore_name']} — {when}. Коли зробите, відмітьте в Grocy."))
        elif 0 < days <= lead and "soon" not in sent:
            out.append((cid, "soon", f"Через {days} дн. ({due:%d.%m.%Y}): {c['chore_name']}."))
    return out


def tick(now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(TZ)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_DEFAULT")
    if now.time() < _SEND_AT or not chat_id:
        return
    lead = int(os.environ.get("CHORE_NOTIFY_LEAD_DAYS", "7"))
    chores = grocy_client._req("GET", "/chores")
    state = _load()
    for cid, kind, text in messages(chores, now.date(), lead, state):
        telegram_client.send_message(chat_id, text)
        due = next(c["next_estimated_execution_time"][:10] for c in chores if str(c["chore_id"]) == cid)
        state[cid] = {due: state.get(cid, {}).get(due, []) + [kind]}  # older due dates dropped
        _save(state)


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:  # Grocy/Telegram down — next tick retries, nothing is marked sent
            print(f"chore notify error: {e}", flush=True)
        await asyncio.sleep(_TICK)
