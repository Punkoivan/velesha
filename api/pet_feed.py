"""Daily dog-food consumption from Grocy + "running low" notification (ADR-0062).

Each feeding slot (PET_FEED_SCHEDULE, e.g. "08:00=75,20:00=75" — time=grams)
consumes that many grams of PET_FEED_PRODUCT_ID in Grocy. Grocy stays the
single source of truth: a new bag is just "купив корм 18 кг" via
grocy_add_stock, no extra state to reset.

Slots missed while the server was down are caught up on the next tick
(bounded by _MAX_CATCHUP), driven by `last_fed` in data/pet_feed.json —
first run starts from "now", it never backfills a history it doesn't know.

When stock drops to PET_FEED_WARN_DAYS of daily ration or less: one
Telegram message + the product on Grocy's shopping list. `warned` resets
once stock is back above the threshold (after a purchase).
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
_STATE = pathlib.Path(__file__).parent / "data" / "pet_feed.json"
_TICK = 60
_MAX_CATCHUP = dt.timedelta(days=3)  # longer outage — assume someone noticed, don't dump days of food at once


def _config() -> tuple[int, list[tuple[dt.time, float]], float, float] | None:
    pid = os.environ.get("PET_FEED_PRODUCT_ID")
    sched = os.environ.get("PET_FEED_SCHEDULE")
    if not pid or not sched:
        return None
    slots = []
    for part in sched.split(","):
        t, grams = part.strip().split("=")
        slots.append((dt.time.fromisoformat(t), float(grams)))
    warn_days = float(os.environ.get("PET_FEED_WARN_DAYS", "7"))
    pack = float(os.environ.get("PET_FEED_PACK_G", "0"))
    return int(pid), sorted(slots), warn_days, pack


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(state, ensure_ascii=False))


def due_slots(last: dt.datetime, now: dt.datetime, slots: list[tuple[dt.time, float]]) -> list[tuple[dt.datetime, float]]:
    """Feeding moments in (last, now], oldest first."""
    out = []
    day = last.date()
    while day <= now.date():
        for t, grams in slots:
            at = dt.datetime.combine(day, t, TZ)
            if last < at <= now:
                out.append((at, grams))
        day += dt.timedelta(days=1)
    return out


def tick(now: dt.datetime | None = None) -> None:
    cfg = _config()
    if not cfg:
        return
    pid, slots, warn_days, pack = cfg
    now = now or dt.datetime.now(TZ)
    state = _load()
    if "last_fed" not in state:
        state["last_fed"] = now.isoformat()
        _save(state)
        return
    last = max(dt.datetime.fromisoformat(state["last_fed"]), now - _MAX_CATCHUP)
    due = due_slots(last, now, slots)
    for at, grams in due:
        have = grocy_client.product_stock(pid)
        if have > 0:
            grocy_client.consume(pid, min(grams, have))  # Grocy rejects consuming more than is in stock
        state["last_fed"] = at.isoformat()
        _save(state)  # per slot — a crash mid-catch-up must not re-consume what's done

    daily = sum(g for _, g in slots)
    have = grocy_client.product_stock(pid)
    threshold = daily * warn_days
    if have > threshold:
        if state.get("warned"):
            state["warned"] = False
            _save(state)
        return
    if state.get("warned"):
        return
    days = int(have // daily) if daily else 0
    text = f"Корму для Еббі лишилось {have / 1000:.1f} кг — приблизно на {days} дн."
    if pack > 0:
        text += " Додала його в список покупок у Grocy."
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_DEFAULT")
    if chat_id:
        telegram_client.send_message(chat_id, text)
    if pack > 0:
        grocy_client.shopping_add(pid, pack)
    state["warned"] = True
    _save(state)


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:  # never let one bad tick (Grocy down, etc.) kill the loop
            print(f"pet feed loop error: {e}", flush=True)
        await asyncio.sleep(_TICK)
