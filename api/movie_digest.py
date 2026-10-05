"""Monthly Telegram digest of unwatched films and series, ranked by the household's TMDB ratings (ADR-0076).

The 1st of each month after 10:00: 5 films + 5 series from the last 3 years via
tmdb.discover (watched/rated titles out, Russian productions out, liked-alike up).
Sent once per month (data/movie_digest.json).
"""

import asyncio
import datetime as dt
import json
import os
import pathlib
import zoneinfo

import telegram_client
import tmdb

TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
_STATE = pathlib.Path(__file__).parent / "data" / "movie_digest.json"
_TICK = 1800


def _sent_month() -> str:
    try:
        return json.loads(_STATE.read_text()).get("month", "")
    except (FileNotFoundError, ValueError):
        return ""


def build(now: dt.datetime) -> str:
    since = now.year - 3
    parts = [f"Що подивитись у {['січні', 'лютому', 'березні', 'квітні', 'травні', 'червні', 'липні', 'серпні', 'вересні', 'жовтні', 'листопаді', 'грудні'][now.month - 1]}:"]
    for kind, label in (("movie", "Фільми"), ("series", "Серіали")):
        rows = tmdb.discover(kind, "", since, now.year, 5)
        if rows:
            parts.append(f"\n{label}:")
            parts += [f"• {r['title']} ({r['year']}) — {r['rating']:.1f}. {r['overview']}"
                      + (" (схоже на те, що вам сподобалось)" if r.get("because_liked") else "") for r in rows]
    return "\n".join(parts)


def tick(now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(TZ)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_DEFAULT")
    month = now.strftime("%Y-%m")
    if now.day != 1 or now.hour < 10 or _sent_month() == month or not chat_id or not tmdb.available():
        return
    telegram_client.send_message(chat_id, build(now)[:4000])
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({"month": month}))


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:  # TMDB/Telegram down — retry next tick, nothing marked sent
            print(f"movie digest error: {e}", flush=True)
        await asyncio.sleep(_TICK)
