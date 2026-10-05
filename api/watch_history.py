"""Household watch history that survives deleting files from Jellyfin (ADR-0075).

Jellyfin forgets an item's "played" mark the moment its file is deleted to
free space, so "what haven't we watched yet" can't be asked of Jellyfin.
This keeps its own record in data/watch.db, one row per movie or series,
keyed by IMDb id (else TMDB, else a normalised title).

Sources, synced weekly (Mondays) and once at startup when a week has passed:
- Jellyfin's activity log (VideoPlayback events) — reaches back about a
  year and still names deleted items;
- items currently marked played.
Deleted items are known by name only (resolved=0) until a later TMDB lookup.
"""

import asyncio
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import zoneinfo

import requests

TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
_DB = pathlib.Path(__file__).parent / "data" / "watch.db"
_TICK = 3600
_SYNC_WEEKDAY, _SYNC_HOUR = 0, 6  # Monday 06:00


def _jf() -> tuple[str, dict, str]:
    url = os.environ["JELLYFIN_URL"].rstrip("/")
    return url, {"Authorization": f'MediaBrowser Token="{os.environ["JELLYFIN_API_KEY"]}"'}, os.environ["JELLYFIN_USER_ID"]


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS watched (
        key TEXT PRIMARY KEY, kind TEXT, title TEXT, year INTEGER, imdb_id TEXT, tmdb_id TEXT,
        plays INTEGER DEFAULT 0, first_watched TEXT, last_watched TEXT, resolved INTEGER DEFAULT 0, raw TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS seen_events (event_id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _norm(title: str) -> str:
    t = re.sub(r"^\d+\.\s*", "", title.lower())  # "02. The Mummy Returns" — collection numbering
    return re.sub(r"[^\w]+", " ", t).strip()


def _key(info: dict) -> str:
    if info.get("imdb_id"):
        return f"imdb:{info['imdb_id']}"
    if info.get("tmdb_id"):
        return f"tmdb:{info['kind']}:{info['tmdb_id']}"
    return f"name:{_norm(info['title'])}|{info.get('year') or ''}"


def _item_info(item_id: str, cache: dict) -> dict | None:
    """Movie or series behind a played item, with provider ids; None if the item is gone."""
    if item_id in cache:
        return cache[item_id]
    url, h, uid = _jf()
    r = requests.get(f"{url}/Items/{item_id}", headers=h, params={"userId": uid}, timeout=15)
    info = None
    if r.status_code == 200:
        it = r.json()
        if it.get("Type") == "Episode" and it.get("SeriesId"):
            return _item_info(it["SeriesId"], cache) if it["SeriesId"] != item_id else None
        kind = {"Movie": "movie", "Series": "series"}.get(it.get("Type"))
        if kind:
            pid = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
            info = {"kind": kind, "title": it.get("OriginalTitle") or it.get("Name"), "year": it.get("ProductionYear"),
                    "imdb_id": pid.get("imdb"), "tmdb_id": pid.get("tmdb"), "resolved": 1, "raw": it.get("Name")}
    cache[item_id] = info
    return info


def _from_log_name(name: str) -> dict:
    """'tv відтворює Декстер - Одна паршива вівця на Firefox' -> what was playing, best guess."""
    what = re.sub(r"^\S+\s+відтворює\s+", "", name)
    what = re.sub(r"\s+на\s+[^-]*$", "", what).strip()
    left, _, right = what.partition(" - ")
    # "Серіал - Епізод" names the series on the left; "X Collection - Фільм" the film on the right
    title = right if right and "collection" in left.lower() else left or what
    return {"kind": "unknown", "title": title.strip(), "year": None, "imdb_id": None, "tmdb_id": None,
            "resolved": 0, "raw": what}


def _upsert(c: sqlite3.Connection, info: dict, when: str) -> None:
    key = _key(info)
    row = c.execute("SELECT plays, first_watched, last_watched FROM watched WHERE key=?", (key,)).fetchone()
    if row:
        plays, first, last = row
        c.execute("UPDATE watched SET plays=?, first_watched=?, last_watched=? WHERE key=?",
                  (plays + 1, min(first, when), max(last, when), key))
    else:
        c.execute("INSERT INTO watched (key, kind, title, year, imdb_id, tmdb_id, plays, first_watched, last_watched,"
                  " resolved, raw) VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                  (key, info["kind"], info["title"], info.get("year"), info.get("imdb_id"), info.get("tmdb_id"),
                   when, when, info.get("resolved", 0), info.get("raw")))


def sync() -> dict:
    """Pull new playback events + currently played items. Returns counts."""
    url, h, uid = _jf()
    c = _conn()
    cache: dict = {}
    added = 0
    start = 0
    while True:
        page = requests.get(f"{url}/System/ActivityLog/Entries", headers=h,
                            params={"startIndex": start, "limit": 500}, timeout=30).json()
        items = page.get("Items") or []
        for e in items:
            if e.get("Type") != "VideoPlayback":
                continue
            if c.execute("SELECT 1 FROM seen_events WHERE event_id=?", (e["Id"],)).fetchone():
                continue
            info = (_item_info(e["ItemId"], cache) if e.get("ItemId") else None) or _from_log_name(e.get("Name", ""))
            _upsert(c, info, e["Date"][:19])
            c.execute("INSERT INTO seen_events (event_id) VALUES (?)", (e["Id"],))
            added += 1
        start += len(items)
        if not items or start >= page.get("TotalRecordCount", 0):
            break
    # played marks: things watched outside the log's reach (or before it existed)
    played = requests.get(f"{url}/Users/{uid}/Items", headers=h, timeout=30, params={
        "Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Filters": "IsPlayed",
        "Fields": "ProviderIds", "EnableUserData": "true"}).json().get("Items") or []
    marked = 0
    for it in played:
        info = _item_info(it["Id"], cache)
        if not info:
            continue
        key = _key(info)
        if not c.execute("SELECT 1 FROM watched WHERE key=?", (key,)).fetchone():
            when = ((it.get("UserData") or {}).get("LastPlayedDate") or "")[:19] or dt.datetime.now(dt.UTC).isoformat()[:19]
            _upsert(c, info, when)
            marked += 1
    c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('last_sync', ?)", (dt.datetime.now(TZ).isoformat(),))
    c.commit()
    total = c.execute("SELECT count(*), sum(resolved) FROM watched").fetchone()
    c.close()
    return {"events": added, "from_played_marks": marked, "titles": total[0], "resolved": total[1] or 0}


def _due(now: dt.datetime) -> bool:
    c = _conn()
    row = c.execute("SELECT v FROM meta WHERE k='last_sync'").fetchone()
    c.close()
    if not row:
        return True
    last = dt.datetime.fromisoformat(row[0])
    if now - last > dt.timedelta(days=7):
        return True  # missed a Monday (server was down)
    return now.weekday() == _SYNC_WEEKDAY and now.hour >= _SYNC_HOUR and last.date() < now.date()


async def loop() -> None:
    while True:
        try:
            if await asyncio.to_thread(_due, dt.datetime.now(TZ)):
                print(f"watch history sync: {await asyncio.to_thread(sync)}", flush=True)
                import tmdb  # lazy: tmdb imports this module
                if tmdb.available():  # deleted titles known by name only -> TMDB ids
                    print(f"watch history tmdb: {await asyncio.to_thread(tmdb.resolve_history)}", flush=True)
        except Exception as e:  # Jellyfin down — try again next hour
            print(f"watch history sync error: {e}", flush=True)
        await asyncio.sleep(_TICK)


def summary(limit: int = 0) -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT kind, title, year, imdb_id, plays, last_watched, resolved FROM watched "
                     "ORDER BY last_watched DESC" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    c.close()
    return [dict(zip(("kind", "title", "year", "imdb_id", "plays", "last_watched", "resolved"), r)) for r in rows]
