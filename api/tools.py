"""Tools the chat model can call — see ADR-0018.

Three tools, chosen to close the exact gaps found in testing:
- search_knowledge: replaces the old always-on blind RAG injection —
  the model now decides when and what to search for.
- get_live_state: live HA state (the door-sensor bug — RAG only ever
  sees an indexed snapshot, never "right now").
- get_sensor_history: period summaries (kWh used, counter change, on/off
  counts and durations) — computed at query time, ADR-0020.
"""

import asyncio
import datetime
import difflib
import json
import os
import pathlib
import re
import time
import zoneinfo

import requests

import guardrails
import grocy_client
import movies_db
import ha_client
import jellyfin_client
import qbit_client
import toloka_client
import users
from qdrant_client import QdrantClient
from search_backend import COLLECTIONS, CANDIDATE_POOL, CHUNKS_PER_COLLECTION, MAX_CHUNK_CHARS, embed, qdrant_client

# Entities whose friendly_name is real but who are pure device-management
# noise for "what's the current state" questions — never a useful match.
# Judged by device_class/domain, not by entity-id spelling: HA renamed
# sensor.*_batareia to *_batareya and the id-based filter silently stopped
# working (ADR-0027).
_NOISE_CLASSES = {"battery", "identify", "firmware", "update", "restart"}
_NOISE_DOMAINS = {"update", "button", "select"}
_NOISE_SUBSTRINGS = (
    "identifikuvati", "firmware", "child_lock", "power_on_state",
    "backlight_mode", "battery", "batareia",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Семантичний пошук у базі знань Velesha: рецепти (Tandoor), "
                "знімок історії Home Assistant (може бути застарілим — для 'коли востаннє' краще get_sensor_history), Jellyfin "
                "(перегляди фільмів/серіалів), особисті оцінки й відгуки на переглянуті фільми "
                "(watched_movies — для 'порадь щось схоже на X', врахування смаку). НЕ дає живий поточний стан "
                "пристроїв і не рахує суми/дельти — для цього є інші інструменти."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Пошуковий запит"},
                    "source": {
                        "type": "string",
                        "enum": ["ha_history", "jellyfin_library", "tandoor_recipes", "watched_movies", "all"],
                        "description": "Обмежити пошук однією колекцією, або 'all' (за замовчуванням)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_state",
            "description": (
                "Поточне (живе, зараз) значення пристрою, сенсора чи лічильника Home "
                "Assistant за назвою — напр. 'чи двері відчинені', 'яка температура "
                "в спальні', 'скільки DNS-запитів заблоковано AdGuard'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {
                        "type": "string",
                        "description": "Конкретна назва пристрою/сенсора, напр. 'вхідні двері', 'температура спальня' (конкретно, не лише назва пристрою)",
                    }
                },
                "required": ["entity_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sensor_history",
            "description": (
                "Підсумок за ПЕРІОД для пристрою чи сенсора Home Assistant: скільки "
                "електроенергії використано (кВт·год), як змінився лічильник (запити "
                "AdGuard), мін/макс/середнє температури, скільки разів і як довго "
                "пристрій був увімкнений. Для питань 'скільки за день', 'що було вчора', "
                "'за тиждень'. Це ЖИВА історія — для 'коли востаннє вмикали/відчиняли X' бери її з start_date='тиждень'. Історія в HA зберігається лише ~10 днів."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string", "description": "Конкретна назва пристрою/сенсора, напр. 'пралка', 'пралка електроенергія' (для кВт·год), 'DNS запити' (скільки запитів ВСЬОГО пройшло) або 'заблоковані DNS запити' (скільки з них заблоковано) — різні лічильники, 'телевізор'"},
                    "start_date": {"type": "string", "description": "Дата так, як сказав користувач ('18.09', 'вчора', 'сьогодні') або YYYY-MM-DD; РІК НЕ ВИГАДУЙ, якщо користувач його не назвав. 'тиждень' — останні 7 днів (для 'коли востаннє')"},
                    "end_date": {"type": "string", "description": "Необов'язково, включно, той самий формат. Без нього — лише start_date"},
                },
                "required": ["entity_name", "start_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qbittorrent_status",
            "description": (
                "Загальний стан qBittorrent користувача (вже доданих торрентів): скільки віддано і завантажено за сесію та "
                "за весь час, ratio, швидкості, вільне місце, скільки торрентів "
                "роздається/качається/на паузі, скільки з нульовою віддачею."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qbittorrent_list",
            "description": "Список торрентів, які ВЖЕ додані в qBittorrent користувача, за фільтром (назва, ratio, віддано за сесію, категорія). НЕ для пошуку нового фільму.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["zero_ratio", "zero_session", "downloading", "problems"],
                        "description": (
                            "zero_ratio — «роздачі з рейтингом 0»: готові торренти, які за весь час нікому не віддавали (ratio 0) — обирай це за замовчуванням; "
                            "zero_session — ЛИШЕ якщо користувач прямо питає про цю сесію: готові, що не віддавали за сесію; "
                            "downloading — що зараз качається; problems — помилки/пауза"
                        ),
                    },
                },
                "required": ["filter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grocy_stock",
            "description": (
                "Що є в домашніх запасах (Grocy: їжа і господарські товари): скільки чогось, де лежить. "
                "Без query — короткий перелік. Для 'чи є X', 'скільки Y', 'що є з круп/консервів'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Назва чи частина назви продукту, напр. 'цукор', 'круп', 'папір'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grocy_recipes",
            "description": (
                "ЛИШЕ коли питають, чи ВИСТАЧАЄ інгредієнтів у запасах для рецептів, заведених у Grocy "
                "('що я можу приготувати з наявного', 'чого не вистачає на X'). Порада 'що приготувати', "
                "рецепти за змістом чи інгредієнтом (курка, борщ) — це search_knowledge, не цей інструмент."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Частина назви рецепта; порожньо — усі"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_periods",
            "description": (
                "ТРИВАЛОСТІ роботи пристрою: кожен цикл/сеанс окремо з початком, кінцем і тривалістю (та кВт·год за цикл, "
                "якщо є лічильник) — 'скільки тривало останнє прання', 'скільки працювала пралка', 'коли почалось/"
                "закінчилось'. Не для суми кВт·год за день (це get_sensor_history). За замовчуванням — останні ~10 днів."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string", "description": "Пристрій, напр. 'пралка'"},
                    "start_date": {"type": "string", "description": "Початок: YYYY-MM-DD, '18.09', 'вчора', 'тиждень'; порожньо — 10 днів"},
                    "end_date": {"type": "string", "description": "Кінець (необов'язково)"},
                    "threshold": {"type": "number", "description": "Поріг активності (для потужності Вт, за замовчуванням 5)"},
                    "merge_gap_minutes": {"type": "integer", "description": "Паузи коротші за це склеювати в один цикл (за замовчуванням 10)"},
                },
                "required": ["entity_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_watched_movie",
            "description": (
                "Записати особисту оцінку й відгук на переглянутий фільм/серіал (SQLite + індексація для рекомендацій). "
                "Файл із Jellyfin згодом видаляється (місце обмежене), тому НЕ прив'язуй запис до jellyfin_id — використовуй "
                "стійкі дані: title — ОРИГІНАЛЬНА назва (англійська/мовою виробництва, не український дубляж; знаєш сам або "
                "бери provider_ids з jellyfin_get_item і imdb_id), year, і, якщо можеш визначити, imdb_id та режисера — "
                "цього досить для однозначної ідентифікації навіть без Jellyfin. Жанри — з jellyfin_get_item, не вигадуй. "
                "Повторний запис того самого фільму (за imdb_id або назвою+роком) — оновлює оцінку. Лише за прямим проханням "
                "оцінити/записати."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Оригінальна назва фільму, не локалізована"},
                    "year": {"type": "integer"},
                    "imdb_id": {"type": "string", "description": "напр. tt21454134, з provider_ids.Imdb у jellyfin_get_item, якщо відомий"},
                    "director": {"type": "string", "description": "Режисер, якщо відомий"},
                    "genres": {"type": "array", "items": {"type": "string"}, "description": "Жанри з метаданих Jellyfin"},
                    "rating": {"type": "number", "description": "Особиста оцінка 0–10"},
                    "review": {"type": "string", "description": "Короткий відгук користувача, якщо був"},
                },
                "required": ["title", "year", "rating"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watched_movies",
            "description": "Історія особистих оцінок переглянутих фільмів (SQLite), фільтр за жанром чи мінімальною оцінкою.",
            "parameters": {
                "type": "object",
                "properties": {
                    "genre": {"type": "string"},
                    "min_rating": {"type": "number"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remind_me",
            "description": (
                "Поставити разове нагадування — надішле пуш-сповіщення на телефон у вказаний час. "
                "Вкажи time (година, напр. '19' чи '19:30') і, якщо треба, date ('сьогодні'/'завтра'/YYYY-MM-DD, "
                "за замовчуванням сьогодні, а якщо цей час уже минув — завтра); або in_minutes замість time/date "
                "для 'через N хвилин'. Лише за прямим проханням нагадати."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Що нагадати, як сказав користувач"},
                    "time": {"type": "string", "description": "Година, напр. '19' або '19:30'"},
                    "date": {"type": "string", "description": "'сьогодні', 'завтра' або YYYY-MM-DD"},
                    "in_minutes": {"type": "integer", "description": "Альтернатива time/date: через скільки хвилин (1–10080)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reminders",
            "description": "Список запланованих нагадувань (найближчі 14 днів).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": (
                "Скасувати активне нагадування АБО заплановану дію пилососа (vacuum_schedule) за частиною тексту "
                "(спершу глянь get_reminders, якщо не певен формулювання — там видно й те, й те). "
                "Щоб ПЕРЕНЕСТИ/ЗМІНИТИ — скасуй старе цим інструментом і постав нове через remind_me/vacuum_schedule. "
                "Лише за прямим проханням скасувати/видалити/прибрати."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Частина тексту нагадування, як сказав користувач"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grocy_shopping_list",
            "description": "Поточний список покупок у Grocy.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _score(hint: str, friendly_name: str) -> float:
    hint_l, name_l = hint.lower(), friendly_name.lower()
    if hint_l in name_l or name_l in hint_l:
        return 1.0
    # Word overlap handles hints that reorder/drop words of a long name
    # ("заблоковані DNS-запити AdGuard" vs "AdGuard Home Заблоковані DNS-запити").
    tokens = re.findall(r"\w+", hint_l)
    overlap = sum(1 for tok in tokens if tok in name_l) / len(tokens) if tokens else 0.0
    return max(overlap * 0.95, difflib.SequenceMatcher(None, hint_l, name_l).ratio())


# A bare device name ("телевізор") matches several entities equally well
# (the switch itself, plus its Струм/Напруга/Summation delivered
# diagnostic sensors) — prefer the entity you'd actually mean by "стан
# пристрою" over its diagnostics when scores tie. Lower sorts first.
_DOMAIN_PRIORITY = {
    "switch": 0, "light": 0, "climate": 0, "lock": 0, "cover": 0,
    "binary_sensor": 0, "media_player": 0, "person": 0, "alarm_control_panel": 0,
}


# Raw "on"/"off" confused the 3B model (answered a switch question with
# an unrelated counter); Ukrainian words in the tool output fix that.
_STATE_UK = {"on": "увімкнено", "off": "вимкнено", "unavailable": "недоступно", "unknown": "невідомо"}


_OPENING_CLASSES = {"door", "window", "opening", "garage_door", "garage"}


def _state_uk(entity: dict) -> str:
    state = entity.get("state")
    device_class = entity.get("attributes", {}).get("device_class")
    if entity["entity_id"].startswith("binary_sensor.") and device_class in _OPENING_CLASSES:
        return {"on": "відчинено", "off": "закрито"}.get(state, state)
    if entity["entity_id"].startswith("binary_sensor.") and device_class == "motion":
        return {"on": "рух є", "off": "руху немає"}.get(state, state)
    return _STATE_UK.get(state, state)


def _fmt_since(iso: str | None) -> str:
    if not iso:
        return "невідомо"
    ts = datetime.datetime.fromisoformat(iso).astimezone(_TZ)
    now = datetime.datetime.now(_TZ)
    return ts.strftime("%H:%M") if ts.date() == now.date() else ts.strftime("%d.%m %H:%M")


def resolve_entities(hint: str, unit: str | None = None, limit: int = 1) -> list[dict]:
    states = ha_client.get_states()
    candidates = []
    for s in states:
        name = s.get("attributes", {}).get("friendly_name")
        if not name:
            continue
        entity_id = s["entity_id"]
        if any(noise in entity_id.lower() for noise in _NOISE_SUBSTRINGS):
            continue
        if entity_id.split(".", 1)[0] in _NOISE_DOMAINS or s.get("attributes", {}).get("device_class") in _NOISE_CLASSES:
            continue
        if unit and s.get("attributes", {}).get("unit_of_measurement") != unit:
            continue
        domain_priority = _DOMAIN_PRIORITY.get(entity_id.split(".", 1)[0], 1)
        candidates.append((_score(hint, name), domain_priority, s))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    if not candidates or candidates[0][0] <= 0.3:
        return []
    # Only near-ties with the best match: a clear winner is returned
    # alone (a 3B model drifts into unrelated entities when handed five
    # loosely-related lines), a genuine tie returns several to choose from.
    best = candidates[0][0]
    near = [s for score, _, s in candidates if score >= best - 0.1][:limit]
    # Duplicate sensors of one physical thing can disagree (a stale
    # "вхідні двері Відкриття" said closed for hours while the real one said
    # open): the most recently changed one is the one to trust.
    return sorted(near, key=lambda s: s.get("last_changed", ""), reverse=True)


def resolve_entity(hint: str, unit: str | None = None) -> dict | None:
    found = resolve_entities(hint, unit, limit=1)
    return found[0] if found else None


def tool_search_knowledge(args: dict) -> str:
    query = args.get("query", "")
    source = args.get("source", "all")
    collections = [source] if source in COLLECTIONS else COLLECTIONS

    client: QdrantClient = qdrant_client()
    vector = embed(query)

    lines = []
    for collection in collections:
        if not client.collection_exists(collection):
            continue
        results = client.query_points(
            collection_name=collection, query=vector, limit=CANDIDATE_POOL, with_payload=True
        ).points
        results = sorted(results, key=lambda r: r.payload.get("last_changed") or "", reverse=True)
        for r in results[:CHUNKS_PER_COLLECTION]:
            text = " ".join(r.payload.get("text", "").split())[:MAX_CHUNK_CHARS]
            lines.append(f"[{collection}] {text}")

    return "\n".join(lines) if lines else "Нічого не знайдено."


def tool_get_live_state(args: dict) -> str:
    # Several entities often match one device name (AdGuard alone has ~14
    # sensors/switches) and silently picking one gave wrong answers in
    # testing — return the top few with their states and let the model
    # choose the one the question is actually about. See ADR-0019.
    hint = args.get("entity_name", "")
    entities = resolve_entities(hint, limit=5)
    if not entities:
        return f"Не знайдено пристрій '{hint}'."
    lines = []
    for e in entities:
        attrs = e.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "")
        state = _state_uk(e)
        since = _fmt_since(e.get("last_changed"))
        lines.append(f"{attrs.get('friendly_name')}: {state} {unit}".rstrip() + f" (змінилось {since})")
    if len(lines) > 1:
        lines.append("Якщо показання різняться — вірне те, що змінилось пізніше (перший рядок).")
    return "\n".join(lines)


try:
    _TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
except Exception:  # tzdata missing — fixed EEST offset, wrong only outside summer time
    _TZ = datetime.timezone(datetime.timedelta(hours=3))


_WEEK_WORDS = {"тиждень", "тиждень", "7 днів", "останні 7 днів", "week", "останній тиждень"}


_DM_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")


def _parse_day(text: str) -> datetime.date | None:
    text = (text or "").strip().lower()
    today = datetime.datetime.now(_TZ).date()
    if text in ("сьогодні", "today"):
        return today
    if text in ("вчора", "yesterday"):
        return today - datetime.timedelta(days=1)
    # "18.09" / "18.09.2026": models guess the year wrong (asked for 18.09
    # they sent 2023/2024 despite today's date in the prompt), so a year the
    # user never gave is resolved here, in code — the latest such date not
    # in the future. See ADR-0026.
    m = _DM_RE.match(text)
    try:
        if m:
            day, month, year = int(m[1]), int(m[2]), m[3]
            if year:
                return datetime.date(int(year) + (2000 if len(year) == 2 else 0), month, day)
            d = datetime.date(today.year, month, day)
            return d if d <= today else datetime.date(today.year - 1, month, day)
        d = datetime.date.fromisoformat(text)
        if (today - d).days > 365:
            # The model also sends ISO dates with a stale year ("2023-09-18");
            # HA keeps far less than a year, so such a date can only mean the
            # latest occurrence of that day (ADR-0036).
            try:
                d = d.replace(year=today.year)
                if d > today:
                    d = d.replace(year=today.year - 1)
            except ValueError:  # 29 Feb
                return None
        return d
    except ValueError:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def tool_get_sensor_history(args: dict) -> str:
    # Arithmetic is done here, not by the model: a 3B model can't reliably
    # subtract or compare timestamps (see ADR-0017/0018). See ADR-0020.
    hint = args.get("entity_name", "")
    # A bare device name ("пралка") resolves to its on/off switch by design
    # (domain priority); words about electricity mean its kWh counter.
    wants_energy = any(w in hint.lower() for w in ("квт", "kwh", "енерг", "спожив", "електр"))
    entity = resolve_entity(hint, unit="kWh") if wants_energy else None
    entity = entity or resolve_entity(hint)
    if not entity:
        return f"Не знайдено пристрій '{hint}'."
    name = entity["attributes"].get("friendly_name")

    raw_start = (args.get("start_date") or "").strip().lower()
    if raw_start in _WEEK_WORDS:  # "коли востаннє X" needs a window, not one day
        today = datetime.datetime.now(_TZ).date()
        first_day, args = today - datetime.timedelta(days=7), {**args, "end_date": today.isoformat()}
    else:
        first_day = _parse_day(args.get("start_date", ""))
    if not first_day:
        return f"Не вдалось розпізнати дату '{args.get('start_date')}' — очікую YYYY-MM-DD, 'сьогодні' або 'вчора'."
    last_day = _parse_day(args["end_date"]) if args.get("end_date") else first_day
    if not last_day or last_day < first_day:
        return f"Некоректний кінець періоду '{args.get('end_date')}'."

    start = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=_TZ)
    end = min(datetime.datetime.combine(last_day, datetime.time.min, tzinfo=_TZ) + datetime.timedelta(days=1),
              datetime.datetime.now(_TZ))
    period = first_day.isoformat() if first_day == last_day else f"{first_day} — {last_day}"

    history = ha_client.get_history(entity["entity_id"], start.isoformat(), end.isoformat())
    points = []
    for h in history:
        ts = datetime.datetime.fromisoformat(h["last_changed"]).astimezone(_TZ)
        points.append((max(ts, start), h.get("state")))
    points = [(t, v) for t, v in points if v not in (None, "unknown", "unavailable")]
    if not points:
        return f"Немає даних по '{name}' за {period} (HA зберігає історію ~10 днів)."

    attrs = entity["attributes"]
    unit = attrs.get("unit_of_measurement", "")
    nums = [(t, _num(v)) for t, v in points]

    # on/off devices: count activations and total time on
    if all(v is None for _, v in nums):
        on_count, on_time, last_on = 0, datetime.timedelta(), None
        for i, (t, v) in enumerate(points):
            nxt = points[i + 1][0] if i + 1 < len(points) else end
            if v == "on":
                on_time += nxt - t
                if i == 0 or points[i - 1][1] != "on":
                    on_count += 1
                last_on = t
        hours, rem = divmod(int(on_time.total_seconds()), 3600)
        text = f"{name} за {period}: вмикався {on_count} раз(ів), сумарно увімкнений {hours} год {rem // 60} хв"
        if last_on:
            text += f", останній раз увімкнений {last_on.strftime('%d.%m %H:%M')}"
        return text + "."

    values = [v for _, v in nums if v is not None]
    first, last = values[0], values[-1]
    counter = attrs.get("state_class") == "total_increasing" or unit in ("kWh", "Wh")
    if counter:
        used, prev = 0.0, None
        for v in values:
            if prev is not None:
                used += v - prev if v >= prev else v  # drop below prev = counter reset
            prev = v
        return f"{name} за {period}: приріст лічильника {_fmt(used)} {unit}."

    change = last - first
    if unit in ("queries", "requests"):
        # one plain sentence — with min/max/mean the 3B model read the start
        # value as the answer to "how many" (ADR-0020)
        return f"{name} за {period}: додалось {_fmt(change)} {unit}."
    return (
        f"{name} за {period}: зміна за період {'+' if change >= 0 else ''}{_fmt(change)} {unit} "
        f"(з {_fmt(first)} до {_fmt(last)}); мін {_fmt(min(values))}, макс {_fmt(max(values))}, "
        f"середнє {_fmt(sum(values) / len(values))}."
    )


# State-changing tools are only *offered* to the model when the user's
# message contains an explicit command verb. Prompt wording alone failed
# in testing: the 3B model called the play tool on "що є з Декстера?" and
# "порадь серіал на вечір" (ADR-0021). Deliberately a code gate, not a
# model decision.
CONTROL_TOOLS = {"qbittorrent_add", "toloka_add",
                 "grocy_consume", "grocy_add_stock", "grocy_shopping_add", "grocy_product_add",
                 "grocy_recipe_consume", "grocy_recipe_shopping", "grocy_recipe_import", "recipe_add",
                 "note_add", "ha_switch", "vacuum_control", "vacuum_schedule",
                 "log_watched_movie", "remind_me", "cancel_reminder"}

# Answered verbatim, without a second model call (ADR-0024).
# Administration stays with the admin (ADR-0035); everything else is shared.
ADMIN_TOOLS = {"qbittorrent_status", "qbittorrent_list", "qbittorrent_add", "toloka_search", "toloka_add"}
PASSTHROUGH_TOOLS = {"toloka_search", "grocy_recipe_import", "recipe_add", "recipe_cook"}
_POWER_RE = re.compile(r"(вимкн|вимик|виключ|погас|увімкн|ввімкн|включ|вруб|запал)", re.IGNORECASE)
_TIMER_RE = re.compile(r"(таймер|заплан|скасу|відміни|відмін)", re.IGNORECASE)
_VACUUM_RE = re.compile(r"(робот|пилосос|роборок|roborock|прибир)", re.IGNORECASE)
_COMMAND_RE = re.compile(
    r"(включ|увімкн|ввімкн|запуст|постав|відтвор|\bplay\b|пауз|продовж|зупин|стоп|наступн|попередн|далі|пропуст|\bnext\b|\bstop\b|\bpause\b)",
    re.IGNORECASE,
)
# "Розкажи, що ти вмієш" — without this, the system prompt only describes
# tools this exact message happens to trigger, so a generic capability
# question got an incomplete answer (no reminders/notes/device control/
# recipes mentioned, since none of THEIR gates matched either).
_CAPABILITIES_RE = re.compile(
    r"(що ти вмієш|на що ти здатн|які твої можливост|що вмієш|розкажи про себе|"
    r"що (ти )?можеш|список (твоїх )?можливостей|яких функці)", re.IGNORECASE)


_TORRENT_LINK_RE = re.compile(r"(magnet:\?\S+|https?://\S+)", re.IGNORECASE)


def _qbit_add_schema() -> dict:
    cats = [n for n, c in qbit_client.categories().items() if c.get("savePath")]
    return {
        "type": "function",
        "function": {
            "name": "qbittorrent_add",
            "description": (
                "Додати торрент у qBittorrent за magnet/посиланням З ПОВІДОМЛЕННЯ користувача, "
                "в категорію (від неї залежить папка). Категорію обирай ЛИШЕ якщо користувач "
                "її назвав; якщо не назвав — не викликай, а запитай."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "magnet або URL .torrent, дослівно з повідомлення"},
                    "category": {"type": "string", "enum": cats, "description": "Категорія (папка збереження)"},
                },
                "required": ["url", "category"],
            },
        },
    }


# Picking from the list the user was just shown is an add request even without
# the word "додай" ("давай перший варіант", "бери 2") — the first version only
# matched add verbs and the tool silently wasn't offered (ADR-0030). Safe to be
# broad: it also needs a fresh search, a category in the user's own words and
# room on the disk.
_ADD_VERB_RE = re.compile(
    r"(додай|додати|завантаж|скач|качай|постав|давай|бери|візьми|обер|вибер|варіант|номер|№|\b[1-8]\b|"
    r"перш|друг|трет|четвер|п'ят)", re.IGNORECASE)
_GROCY_CONSUME_RE = re.compile(r"(використав|використала|витратив|витратила|списав|списала|з'їв|з'їла|випив|випила|закінчив|закінчил)", re.IGNORECASE)
_GROCY_ADD_RE = re.compile(r"(купив|купила|докупив|докупила|поклав|поклала|поповни|прибав|додай до запас|додати до запас)", re.IGNORECASE)
# Broader than _GROCY_ADD_RE on purpose: creating a brand-new product is a
# different (and less predictable) natural phrasing than restocking an
# existing one ("додай товар", "заведи в перелік", "додай в grocy") —
# offered alongside grocy_add_stock either way, and the tool itself refuses
# if the product already exists.
_GROCY_NEW_PRODUCT_RE = re.compile(
    r"(дода|занес|завед|створ).*(товар|перелік|(grocy|гроч|грок))|"
    r"(товар|перелік|(grocy|гроч|грок)).*(дода|занес|завед|створ)", re.IGNORECASE)
_GROCY_SHOP_RE = re.compile(r"(список покупок|списку покупок|треба купити|потрібно купити|купити)", re.IGNORECASE)

_GROCY_COOK_RE = re.compile(r"(приготував|приготувала|зварив|зварила|спік|спекла|засмажив|засмажила)", re.IGNORECASE)
_GROCY_RSHOP_RE = re.compile(r"(список покупок|списку покупок|додай.*не вистача|не вистача.*додай)", re.IGNORECASE)
_GROCY_IMPORT_RE = re.compile(
    r"(дода|перенес|занес|імпорт|закин|запиш|запам'?ята|занот|збере).*рецепт|"
    r"рецепт.*(дода|перенес|занес|імпорт|закин|запиш|запам'?ята|занот|збере)|"
    r"рецепт.*(grocy|гроч|грок)|(grocy|гроч|грок).*рецепт", re.IGNORECASE)
_CONFIRM_RE = re.compile(
    r"(так\b|\bок\b|окей|добре|давай|підтверджую|запис(уй|уємо|ати)|роби|створюй|додавай|додай|"
    r"вірно|правильно|норм)", re.IGNORECASE)
_recipe_state: dict[str, dict] = {}


def _rs() -> dict:
    return _recipe_state.setdefault(users.current(), {"draft": None, "at": 0.0, "ctx_at": 0.0})


# HA drops a conversation after a few idle minutes and the next short reply
# ("Ранч") then arrives with no history; remember that a Tandoor→Grocy import
# is in progress so that reply is routed to it, not to a film search (ADR-0033).
_IMPORT_CTX_TTL = 60 * 60


def _fresh_import_ctx() -> bool:
    return time.time() - _rs()["ctx_at"] < _IMPORT_CTX_TTL


def in_recipe_import(text: str) -> bool:
    """A recipe import is under way and the reply is short: a bare dish name, not a film title."""
    return _fresh_import_ctx() and len((text or "").split()) <= 4


def _fresh_recipe_draft() -> bool:
    return _rs()["draft"] is not None and time.time() - _rs()["at"] < _PENDING_TTL

_LOG_MOVIE_RE = re.compile(r"(оцін|подивив|подивилас|подивилис|переглянут|рецензі|відгук|додай.*(перегляд|фільм))", re.IGNORECASE)
_REMIND_RE = re.compile(r"нагад", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2})[:.]?(\d{2})?$")
_SEARCH_TTL = 30 * 60
_last_search: dict = {"at": 0.0, "rows": []}
# A variant the user already picked whose category is still missing. The agent
# asks "в яку категорію?" and the reply is a bare "фільми" — a message with no
# verb, number or list word, which the per-message gate used to drop (ADR-0031).
_PENDING_TTL = 10 * 60
_pending_add: dict = {"number": None, "at": 0.0}


def _fresh_pending() -> bool:
    return _pending_add["number"] is not None and time.time() - _pending_add["at"] < _PENDING_TTL


def _names_a_category(text: str) -> bool:
    low = text.lower()
    return any(name.lower()[:5] in low for name, c in qbit_client.categories().items() if c.get("savePath"))


def _fresh_search() -> bool:
    return bool(_last_search["rows"]) and time.time() - _last_search["at"] < _SEARCH_TTL


def _toloka_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "toloka_search",
            "description": ("Пошук НОВОЇ роздачі на Толоці за назвою фільму чи серіалу, щоб його завантажити: повертає варіанти з розміром і сідерами. "
                            "Викликай, коли користувач хоче знайти/скачати/подивитись фільм чи серіал, якого немає в його бібліотеці."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Назва фільму/серіалу, можна рік, напр. 'Декстер' чи 'Mandy 2018'"}},
                "required": ["query"],
            },
        },
    }


def _toloka_add_schema() -> dict:
    cats = [n for n, c in qbit_client.categories().items() if c.get("savePath")]
    return {
        "type": "function",
        "function": {
            "name": "toloka_add",
            "description": (
                "Додати в qBittorrent варіант з ОСТАННЬОГО пошуку на Толоці за його номером у списку. "
                "Категорію (папку) обирай ЛИШЕ якщо користувач її назвав; інакше не викликай, а запитай."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Номер варіанта зі списку пошуку. Не вказуй, якщо користувач обрав його раніше, а зараз лише назвав категорію"},
                    "category": {"type": "string", "enum": cats, "description": "Категорія (папка збереження)"},
                },
                "required": ["category"],
            },
        },
    }


def _grocy_action_schema(name: str, desc: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {"type": "string", "description": "Назва продукту"},
                    "amount": {"type": "number", "description": "Кількість В ОДИНИЦЯХ ПРОДУКТУ у Grocy (спершу подивись grocy_stock, якщо не певен: кг чи г, л чи мл)"},
                },
                "required": ["product", "amount"],
            },
        },
    }


def _grocy_recipe_schema(name: str, desc: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {"recipe": {"type": "string", "description": "Назва рецепта з Grocy"}},
                "required": ["recipe"],
            },
        },
    }


_GROCY_MISSING_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grocy_recipes_not_imported",
        "description": (
            "Які рецепти з Tandoor ще НЕ додані до Grocy (точний перелік, рахується кодом; "
            "не використовуй для цього search_knowledge)."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}



_HA_SWITCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ha_switch",
        "description": (
            "Увімкнути або вимкнути пристрій (розетку/перемикач) зі списку дозволених, напр. телевізор. "
            "when=now (за замовчуванням): зараз. when=after_playback: вимкнути, коли закінчиться фільм на Kodi "
            "(агент бере залишок з Jellyfin і ставить таймер HA). when=in_minutes + minutes: вимкнути через N хвилин. "
            "when=status: що заплановано. when=cancel: скасувати таймер. Лише за прямим проханням."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Назва пристрою, як сказав користувач (напр. «телевізор»)"},
                "state": {"type": "string", "enum": ["on", "off"]},
                "when": {"type": "string", "enum": ["now", "after_playback", "in_minutes", "status", "cancel"]},
                "minutes": {"type": "integer", "description": "Для in_minutes: через скільки хвилин"},
            },
            "required": ["device"],
        },
    },
}


_GROCY_IMPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grocy_recipe_import",
        "description": (
            "Перенести рецепт з Tandoor у Grocy (зі структурованими інгредієнтами). Спершу без confirm — "
            "повертає чернетку на підтвердження. Виклик з confirm=true лише коли користувач підтвердив чернетку."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipe": {"type": "string", "description": "Рівно те, що сказав користувач (напр. «соус», «Ранч»). НЕ вибирай рецепт сам: якщо назва неоднозначна, передай як є, інструмент перепитає."},
                "confirm": {"type": "boolean", "description": "true — записати підтверджену чернетку"},
            },
            "required": ["recipe"],
        },
    },
}


_GROCY_RECIPE_SCHEMAS = {
    "grocy_recipe_consume": ("Позначити, що рецепт з Grocy приготовано: списати його інгредієнти зі запасів. Лише коли користувач каже, що приготував/зварив.", _GROCY_COOK_RE),
    "grocy_recipe_shopping": ("Додати до списку покупок Grocy те, чого не вистачає для рецепта. Лише за проханням додати нестачу в список покупок.", _GROCY_RSHOP_RE),
}


_GROCY_SCHEMAS = {
    "grocy_consume": ("Списати зі запасів використане/витрачене (Grocy). Лише коли користувач каже, що використав/витратив/з'їв.", _GROCY_CONSUME_RE),
    "grocy_add_stock": ("Додати до запасів куплене/поповнене (Grocy). Лише коли користувач каже, що купив/докупив/поклав.", _GROCY_ADD_RE),
    "grocy_shopping_add": ("Додати продукт до списку покупок Grocy (запас не змінює). Лише коли користувач просить додати в список покупок / що треба купити.", _GROCY_SHOP_RE),
}


def tools_for(user_text: str) -> list[dict]:
    text = user_text or ""
    # "Розкажи, що ти вмієш" needs every tool visible at once, not just the
    # ones this exact phrasing happens to trigger — every gate below also
    # opens for it.
    show_all = bool(_CAPABILITIES_RE.search(text))
    tools = TOOLS if (_COMMAND_RE.search(text) or show_all) \
        else [t for t in TOOLS if t["function"]["name"] not in CONTROL_TOOLS]
    # Adding a torrent needs an actual link in the user's own message —
    # a model can't be allowed to invent one (ADR-0023).
    if show_all or _TORRENT_LINK_RE.search(text):
        tools = tools + [_qbit_add_schema()]
    # Read-only and cheap, so always offered: a word gate looked only at the
    # latest message and lost the request in multi-turn talk (ADR-0029).
    tools = tools + [_toloka_search_schema(), _get_reminders_schema(), _notes_search_schema(), _recipe_cook_schema()]
    # grocy_recipe_import/recipe_add write, but only behind their own
    # draft+confirm+title-match gate (never on the first call) — so, same
    # reasoning as ADR-0029, always offered rather than word-gated. A regex
    # gate here kept missing real phrasing ("запишемо" vs "запиши", a
    # "рецепт" mentioned turns ago, a 5-word confirmation) and the model
    # fell back to note_add, the only tool it still had.
    tools = tools + [_GROCY_IMPORT_SCHEMA, _GROCY_MISSING_SCHEMA, _recipe_add_schema()]
    # get_watched_movies is already unconditionally in TOOLS (not a CONTROL_TOOL) — do not re-add it.
    if show_all or _LOG_MOVIE_RE.search(text):
        tools = tools + [_log_watched_movie_schema()]
    for name, (desc, gate) in _GROCY_SCHEMAS.items():
        if show_all or gate.search(text):  # state-changing: only on an explicit phrase (ADR-0032)
            tools = tools + [_grocy_action_schema(name, desc)]
    if show_all or _GROCY_ADD_RE.search(text) or _GROCY_NEW_PRODUCT_RE.search(text):
        # grocy_add_stock only works on an existing product, so the model
        # tries it first and falls back to this when told the product
        # doesn't exist yet.
        tools = tools + [_grocy_product_add_schema()]
    short_reply = len(text.split()) <= 4
    if _GROCY_IMPORT_RE.search(text):
        # Keeps the 60-min "recipe conversation" window alive (_fresh_import_ctx,
        # ADR-0033) even on a turn where the model doesn't call a tool yet
        # (still gathering title/ingredients) — used below and in adk_agent.py
        # to keep a bare dish name from being mistaken for a film title.
        _rs()["ctx_at"] = time.time()
    in_import = _fresh_import_ctx() and short_reply
    if in_import:  # a bare dish name mid-import is not a film title or a torrent
        tools = [x for x in tools if x["function"]["name"] not in ("toloka_search",)]
    for name, (desc, gate) in _GROCY_RECIPE_SCHEMAS.items():
        if show_all or gate.search(text):
            tools = tools + [_grocy_recipe_schema(name, desc)]
    if show_all or _POWER_RE.search(text) or _TIMER_RE.search(text):
        tools = tools + [_HA_SWITCH_SCHEMA]
    if show_all or _VACUUM_RE.search(text):
        tools = tools + [_vacuum_schema(), _vacuum_schedule_schema()]
    if show_all or _REMIND_RE.search(text):
        tools = tools + [_remind_me_schema()]
    # cancel_reminder also cancels a scheduled vacuum_schedule run (same
    # calendar) — offered on _TIMER_RE too ("скасуй"/"відміни"/"заплан"),
    # not just the word "нагад", so "скасуй заплановане прибирання" reaches it.
    if show_all or _REMIND_RE.search(text) or _TIMER_RE.search(text):
        tools = tools + [_cancel_reminder_schema()]
    if (show_all or _WEB_RE.search(text)) and web_search_available():
        tools = tools + [_web_search_schema()]
    # Adding from Toloka works only on a variant the user was just shown.
    if show_all or (_fresh_search() and (_ADD_VERB_RE.search(text) or _fresh_pending() or _names_a_category(text))):
        tools = tools + [_toloka_add_schema()]
    if show_all or _NOTE_ADD_RE.search(text):
        tools = tools + [_note_add_schema()]
    if not users.is_admin():
        tools = [x for x in tools if x["function"]["name"] not in ADMIN_TOOLS]
    # Defence in depth: a tool baked into the base TOOLS list plus its own gated
    # re-add (a mistake this file has made before, ADR-0045) must never reach the
    # model twice — ADK logs a warning and shadows the second one.
    seen, deduped = set(), []
    for tool in tools:
        name = tool["function"]["name"]
        if name not in seen:
            seen.add(name)
            deduped.append(tool)
    return deduped


# The only device this tool may ever start playback on — a state-changing
# tool gets an allowlist, not "any session" (ADR-0021).








_PLAYSTATE = {"pause": "Pause", "resume": "Unpause", "stop": "Stop"}




def _gib(n: float) -> str:
    return f"{n / 1024**3:.1f} ГБ" if n < 1024**4 else f"{n / 1024**4:.2f} ТБ"


_DOWNLOADING = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "allocating", "checkingDL"}
_PAUSED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}
_PROBLEMS = {"error", "missingFiles"} | _PAUSED


def _done(t: dict) -> bool:
    return t.get("progress", 0) >= 1


def tool_qbittorrent_status(args: dict) -> str:
    st = qbit_client.server_state()
    ts = qbit_client.torrents()
    done = [t for t in ts if _done(t)]
    zero_ratio = sum(1 for t in done if t["ratio"] == 0)
    zero_session = sum(1 for t in done if t.get("uploaded_session", 0) == 0)
    return (
        f"qBittorrent. Віддано за цю сесію: {_gib(st['up_info_data'])}. "
        f"Віддано загалом (за весь час): {_gib(st['alltime_ul'])}. "
        f"Завантажено за сесію: {_gib(st['dl_info_data'])}, загалом: {_gib(st['alltime_dl'])}. "
        f"Загальний ratio: {st['global_ratio']}. "
        f"Зараз віддача {st['up_info_speed'] / 1024**2:.1f} МБ/с, завантаження {st['dl_info_speed'] / 1024**2:.1f} МБ/с. "
        f"Вільно на диску {_gib(st['free_space_on_disk'])}. "
        f"Торрентів {len(ts)}: качається {sum(1 for t in ts if t['state'] in _DOWNLOADING)}, "
        f"на паузі {sum(1 for t in ts if t['state'] in _PAUSED)}, "
        f"з помилкою {sum(1 for t in ts if t['state'] in ('error', 'missingFiles'))}. "
        f"Роздач з рейтингом 0 (ratio 0, за весь час нікому не віддавали): {zero_ratio}. "
        f"Додатково, без віддачі саме за цю сесію: {zero_session}."
    )


def tool_qbittorrent_list(args: dict) -> str:
    kind = args.get("filter", "")
    ts = qbit_client.torrents()
    if kind == "zero_ratio":
        rows = [t for t in ts if _done(t) and t["ratio"] == 0]
    elif kind == "zero_session":
        rows = [t for t in ts if _done(t) and t.get("uploaded_session", 0) == 0]
    elif kind == "downloading":
        rows = [t for t in ts if t["state"] in _DOWNLOADING]
    elif kind == "problems":
        rows = [t for t in ts if t["state"] in _PROBLEMS]
    else:
        return f"Невідомий фільтр '{kind}'."
    if not rows:
        return "Таких торрентів немає."
    lines = [
        f"{t['name'][:70]} — ratio {t['ratio']:.2f}, віддано за сесію {_gib(t.get('uploaded_session', 0))}, "
        f"віддано загалом {_gib(t.get('uploaded', 0))}, "
        f"категорія {t['category'] or '—'}, {_gib(t['size'])}"
        for t in rows[:10]
    ]
    extra = f"\n… і ще {len(rows) - 10}." if len(rows) > 10 else ""
    return f"Всього {len(rows)}:\n" + "\n".join(lines) + extra


def tool_qbittorrent_add(args: dict, user_text: str) -> str:
    url, category = args.get("url", "").strip(), args.get("category", "")
    if not url or url not in user_text:
        return "Посилання має бути дослівно з повідомлення користувача."
    cats = {n: c for n, c in qbit_client.categories().items() if c.get("savePath")}
    if category not in cats:
        return f"Невідома категорія '{category}'. Доступні: {', '.join(cats)}."
    # The category must come from the user, not from the model's guess: a
    # wrong one puts files in the wrong folder (ADR-0023).
    if category.lower()[:5] not in user_text.lower():
        return f"НЕ ДОДАНО. Користувач не назвав категорію. Запитай його: в яку категорію додати ({', '.join(cats)})?"
    if qbit_client.add(url, category):
        return f"Додано в qBittorrent, категорія «{category}» (папка {cats[category]['savePath']})."
    return "qBittorrent відхилив торрент (можливо, уже додано або посилання некоректне)."


_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _size_bytes(text: str) -> float | None:
    m = re.match(r"\s*([\d.,]+)\s*([KMGT]B)", text, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * _UNIT[m.group(2).upper()]


def _fits(size_text: str, available: float) -> bool:
    size = _size_bytes(size_text)
    return size is None or size <= available


def _available_space() -> tuple[float, float]:
    """(free on disk, free minus what running downloads still have to write).

    qBittorrent's free space ignores torrents mid-download, so two adds in
    a row could each "fit" and together overfill the disk.
    """
    free = qbit_client.server_state()["free_space_on_disk"]
    pending = sum(t.get("amount_left", 0) for t in qbit_client.torrents() if t["state"] in _DOWNLOADING)
    return free, max(free - pending, 0)


def tool_toloka_search(args: dict) -> str:
    query = args.get("query", "").strip()
    if not query:
        return "Не вказано, що шукати."
    rows = sorted(toloka_client.search(query), key=lambda r: -r["seeders"])[:8]
    if not rows:
        return f"На Толоці нічого не знайдено за «{query}»."
    _last_search.update(at=time.time(), rows=rows)
    free, available = _available_space()
    lines = [
        f"{i}. {r['title'][:95]} — {r['size']}, сідерів: {r['seeders']} ({r['forum']})"
        + ("" if _fits(r["size"], available) else " — більше за наявне місце")
        for i, r in enumerate(rows, 1)
    ]
    cats = [n for n, c in qbit_client.categories().items() if c.get("savePath")]
    return (
        f"Знайдено на Толоці за «{query}» (за кількістю сідерів):\n" + "\n".join(lines)
        + f"\n\nДоступно для нових завантажень: {_gib(available)}"
        + (f" (вільно {_gib(free)}, ще докачується {_gib(free - available)})" if free != available else "")
        + f". Щоб додати — скажи номер і розділ ({', '.join(cats)}), напр.: «додай 2 в {cats[0] if cats else 'фільми'}»."
    )


def tool_toloka_add(args: dict, user_text: str) -> str:
    if not _fresh_search():
        return "НЕ ДОДАНО. Немає свіжого пошуку — спершу знайди роздачу на Толоці."
    rows = _last_search["rows"]
    number, category = args.get("number"), args.get("category", "")
    if not isinstance(number, int) and _fresh_pending():
        number = _pending_add["number"]  # user picked earlier, only the category was missing
    if not isinstance(number, int) or not 1 <= number <= len(rows):
        return f"НЕ ДОДАНО. Номер має бути від 1 до {len(rows)}."
    cats = {n: c for n, c in qbit_client.categories().items() if c.get("savePath")}
    if category not in cats:
        _pending_add.update(number=number, at=time.time())
        return f"НЕ ДОДАНО. Невідома категорія '{category}'. Доступні: {', '.join(cats)}."
    # The category must come from the user, not from the model's guess: a
    # wrong one puts files in the wrong folder (ADR-0023/0024).
    if category.lower()[:5] not in user_text.lower():
        _pending_add.update(number=number, at=time.time())
        return f"НЕ ДОДАНО. Користувач не назвав розділ. Запитай його: в яку категорію додати ({', '.join(cats)})?"
    row = rows[number - 1]
    free, available = _available_space()
    if not _fits(row["size"], available):
        shortfall = _size_bytes(row["size"]) - available
        return (
            f"НЕ ДОДАНО. Роздача {row['size']}, а наявного місця {_gib(available)} "
            f"(не вистачає {_gib(shortfall)}). Два варіанти: обрати менший варіант зі списку "
            "або звільнити місце вручну і повторити."
        )
    data = toloka_client.download_torrent(row["download_id"])
    if qbit_client.add_file(data, category):
        _pending_add.update(number=None)
        return f"Додано в qBittorrent: «{row['title'][:80]}» ({row['size']}), категорія «{category}» (папка {cats[category]['savePath']})."
    return "qBittorrent відхилив торрент (можливо, уже додано)."


_WEB_RE = re.compile(
    r"(інтернет|в мережі|онлайн|погугли|загугли|google|пошукай в|шукай в|свіж|нов(ий|і|ин|ішо|енький)|"
    r"останн|актуальн|вийшл|вийде|imdb|рейтинг|2025|2026)", re.IGNORECASE)


def _openai_key() -> str | None:
    """Web search rides on the OpenAI Responses API — only when OpenAI is the working model."""
    return os.environ.get("OPENAI_API_KEY") if os.environ.get("CHAT_PROVIDER") == "openai" else None


def web_search_available() -> bool:
    return bool(_openai_key()) and guardrails.web_searches_left() > 0 and guardrails.budget_left() > 0


def _web_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Пошук в інтернеті: свіжі новини, фільми/серіали що вийшли нещодавно, рейтинги, "
                "будь-що актуальне, чого немає в даних користувача. Викликай, коли користувач "
                "просить пошукати в інтернеті або щось свіже. Запит формулюй по суті, без особистих даних."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Що знайти, напр. 'нові фільми Тарантіно 2026'"}},
                "required": ["query"],
            },
        },
    }


def tool_web_search(args: dict) -> str:
    query = guardrails.redact(args.get("query", "").strip())  # only the query leaves; masked like everything else
    if not query:
        return "Не вказано, що шукати."
    if not web_search_available():
        return "Пошук в інтернеті зараз недоступний (немає ключа або вичерпано денний ліміт)."
    today = datetime.datetime.now(_TZ).strftime("%Y-%m-%d")
    body = {
        "model": os.environ.get("CHAT_MODEL", "gpt-4.1-mini"),
        "tools": [{"type": "web_search"}],
        "instructions": (
            f"Сьогодні {today}. Знайди в інтернеті відповідь на запит і відповідай українською: 3–6 коротких "
            "пунктів з назвами та роками. Лише перевірені факти, без вигадок."
        ),
        "input": query,
        "max_output_tokens": 700,
    }
    r = requests.post("https://api.openai.com/v1/responses", json=body,
                      headers={"Authorization": f"Bearer {_openai_key()}"}, timeout=90)
    if r.status_code >= 400:
        return f"Пошук в інтернеті не вдався (HTTP {r.status_code})."
    data = r.json()
    guardrails.record_web_search(data.get("usage", {}).get("total_tokens", 0))
    texts, sources = [], []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
                for a in part.get("annotations", []):
                    if a.get("type") == "url_citation" and a["url"] not in sources:
                        sources.append(a["url"].split("?")[0])
    if not texts:
        return "Пошук в інтернеті нічого не повернув."
    return "\n".join(texts) + ("\n\nДжерела: " + ", ".join(sources[:3]) if sources else "")




def _grocy_units() -> dict[int, str]:
    return {u["id"]: u["name"] for u in grocy_client.objects("quantity_units")}


def _grocy_match(query: str, strict: bool = False) -> list[dict]:
    """Products best matching a name, best first; only clear matches (score > 0.75).
    strict: every word of the query must match a product word (recipe import —
    "сир домашній" must not match "Компот домашній")."""
    q = query.lower().strip()
    scored = []
    for p in grocy_client.objects("products"):
        name = p["name"].lower()
        if name == q:
            score = 2.0
        else:  # inflected forms ("цукру" for "Цукор"): best word-vs-word similarity
            per_token = []
            for qt in q.split():
                best = 0.0
                for w in name.split():
                    r = 1.0 if w.startswith(qt) else difflib.SequenceMatcher(None, qt, w).ratio()
                    best = max(best, r)
                per_token.append(best)
            score = (min if strict else max)(per_token, default=0)
        if score > 0.75:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored]


def _fmt_amount(a: float) -> str:
    return f"{a:g}"


def tool_grocy_stock(args: dict) -> str:
    units, locs = _grocy_units(), {l["id"]: l["name"] for l in grocy_client.objects("locations")}
    stock = {row["product_id"]: float(row["amount"]) for row in grocy_client.stock()}
    query = (args.get("query") or "").strip()
    products = _grocy_match(query) if query else grocy_client.objects("products")
    if query and not products:
        return f"У запасах Grocy немає нічого схожого на «{query}»."
    lines = []
    for p in products[:25]:
        amt = stock.get(p["id"])
        have = f"{_fmt_amount(amt)} {units.get(p['qu_id_stock'], '')}" if amt else "запасу немає (кількість не вказана)"
        lines.append(f"{p['name']}: {have} ({locs.get(p['location_id'], '?')})")
    more = f"\n… і ще {len(products) - 25}." if len(products) > 25 else ""
    return "\n".join(lines) + more


def tool_grocy_shopping_list(args: dict) -> str:
    units = _grocy_units()
    names = {p["id"]: p for p in grocy_client.objects("products")}
    rows = grocy_client.shopping_list()
    if not rows:
        return "Список покупок порожній."
    return "\n".join(f"{names[r['product_id']]['name']}: {_fmt_amount(float(r['amount']))} {units.get(names[r['product_id']]['qu_id_stock'], '')}" for r in rows if r.get("product_id") in names)


def _grocy_one(product: str):
    """(product, None) for one clear match, else (None, refusal message)."""
    found = _grocy_match(product)
    if not found:
        return None, f"НЕ ЗМІНЕНО. У Grocy немає продукту «{product}»."
    if len(found) > 1 and found[0]["name"].lower() != product.lower().strip():
        # several plausible products and no exact name: never guess which one
        return None, "НЕ ЗМІНЕНО. Уточни, який саме продукт: " + "; ".join(p["name"] for p in found[:5]) + "."
    return found[0], None


def _grocy_change(kind: str, args: dict) -> str:
    amount = args.get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        return "НЕ ЗМІНЕНО. Кількість має бути додатним числом."
    p, err = _grocy_one(args.get("product", ""))
    if err:
        return err
    unit = _grocy_units().get(p["qu_id_stock"], "")
    have = grocy_client.product_stock(p["id"])
    if kind == "consume":
        if amount > have:
            # also catches g-vs-kg slips: 200 (кг) of something with 2 кг in stock
            return f"НЕ ЗМІНЕНО. Списати {_fmt_amount(amount)} {unit}, а в запасі лише {_fmt_amount(have)} {unit} «{p['name']}». Перевір кількість і одиницю."
        grocy_client.consume(p["id"], amount)
        return f"Списано {_fmt_amount(amount)} {unit} «{p['name']}». Залишилось {_fmt_amount(have - amount)} {unit}."
    if kind == "add":
        grocy_client.add_stock(p["id"], amount)
        return f"Додано до запасів {_fmt_amount(amount)} {unit} «{p['name']}». Тепер {_fmt_amount(have + amount)} {unit}."
    grocy_client.shopping_add(p["id"], amount)
    return f"Додано до списку покупок: {_fmt_amount(amount)} {unit} «{p['name']}»."


def tool_grocy_consume(args: dict) -> str:
    return _grocy_change("consume", args)


def tool_grocy_add_stock(args: dict) -> str:
    return _grocy_change("add", args)


def tool_grocy_shopping_add(args: dict) -> str:
    return _grocy_change("shop", args)


def _grocy_product_add_schema() -> dict:
    return {"type": "function", "function": {
        "name": "grocy_product_add",
        "description": (
            "Створити НОВИЙ товар у Grocy з початковим запасом — коли товару ще немає "
            "(grocy_stock/grocy_add_stock кажуть, що його нема). Лише за явним проханням завести/додати товар "
            "з указаною кількістю; для поповнення вже наявного товару — grocy_add_stock."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Назва товару, як сказав користувач"},
                "amount": {"type": "number", "description": "Початкова кількість у запасі (0, якщо просто завести товар)"},
                "unit": {"type": "string", "description": "Одиниця виміру (напр. л, кг, шт); якщо не сказано — 'шт'"},
            },
            "required": ["name", "amount"],
        },
    }}


def tool_grocy_product_add(args: dict) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return "НЕ ЗМІНЕНО. Яка назва товару?"
    amount = args.get("amount")
    if not isinstance(amount, (int, float)) or amount < 0:
        return "НЕ ЗМІНЕНО. Вкажи початкову кількість (0, якщо просто завести товар без запасу)."
    if _grocy_match(name, strict=True):
        return f"НЕ ЗМІНЕНО. Товар «{name}» уже є в Grocy — онови запас через grocy_add_stock."
    unit_name = (args.get("unit") or "шт").strip().lower()
    units = {v.lower(): k for k, v in _grocy_units().items()}
    unit_id = units.get(unit_name) or grocy_client.create("quantity_units", {"name": unit_name, "name_plural": unit_name})
    loc = next((l["id"] for l in grocy_client.objects("locations") if l["name"] == "Кухня"), 1)
    grp = next((g["id"] for g in grocy_client.objects("product_groups") if g["name"] == "Продукти"), None)
    data = {"name": name, "location_id": loc, "qu_id_stock": unit_id, "qu_id_purchase": unit_id,
            "qu_id_consume": unit_id, "qu_id_price": unit_id, "description": "Створено голосом через Velesha"}
    if grp:
        data["product_group_id"] = grp
    pid = grocy_client.create("products", data)
    if amount > 0:
        grocy_client.set_stock(pid, amount, loc, note="Початковий запас через Velesha")
    return f"Створено товар «{name}» у Grocy, початковий запас: {_fmt_amount(amount)} {unit_name}."


def _tandoor_titles() -> list[str]:
    pts, _ = qdrant_client().scroll("tandoor_recipes", limit=500, with_payload=True)
    return sorted(p.payload["title"] for p in pts)


def tool_grocy_recipes_not_imported(args: dict) -> str:
    have = {r["name"].lower() for r in grocy_client.objects("recipes")}
    todo = [t for t in _tandoor_titles() if t.lower() not in have]
    _rs()["ctx_at"] = time.time()
    if not todo:
        return "Усі рецепти з Tandoor уже є в Grocy."
    return f"Ще не в Grocy ({len(todo)} із {len(todo) + len(have)}): " + "; ".join(todo) + "."


def _grocy_recipe_match(query: str) -> list[dict]:
    q = query.lower().strip()
    recipes = grocy_client.objects("recipes")
    exact = [r for r in recipes if r["name"].lower() == q]
    if exact:
        return exact
    words = q.split()
    return [r for r in recipes if all(any(w.startswith(qt) or qt.startswith(w[:max(4, len(qt) - 2)]) for w in r["name"].lower().split()) for qt in words)] if words else recipes


def tool_grocy_recipes(args: dict) -> str:
    query = (args.get("query") or "").strip()
    recipes = [r for r in _grocy_recipe_match(query) if r.get("type") == "normal"]
    if not recipes:
        return ("У Grocy немає рецептів" + (f" за «{query}»" if query else "")
                + ". Для поради чи пошуку рецептів використай search_knowledge (Tandoor).")
    lines = []
    for r in recipes[:15]:
        f = grocy_client.recipe_fulfillment(r["id"])
        if f.get("need_fulfilled"):
            lines.append(f"{r['name']}: усе є в запасах")
        else:
            lines.append(f"{r['name']}: не вистачає {f.get('missing_products_count', '?')} інгр.")
    return "\n".join(lines)


def _grocy_recipe_one(name: str):
    found = [r for r in _grocy_recipe_match(name) if r.get("type") == "normal"]
    if not found:
        return None, f"НЕ ЗМІНЕНО. У Grocy немає рецепта «{name}»."
    if len(found) > 1 and found[0]["name"].lower() != name.lower().strip():
        return None, "НЕ ЗМІНЕНО. Уточни, який саме рецепт: " + "; ".join(r["name"] for r in found[:5]) + "."
    return found[0], None


def tool_grocy_recipe_consume(args: dict) -> str:
    r, err = _grocy_recipe_one(args.get("recipe", ""))
    if err:
        return err
    f = grocy_client.recipe_fulfillment(r["id"])
    if not f.get("need_fulfilled"):
        return f"НЕ ЗМІНЕНО. Для «{r['name']}» у запасах не вистачає інгредієнтів ({f.get('missing_products_count', '?')}), списувати нема з чого."
    grocy_client.recipe_consume(r["id"])
    return f"Списано інгредієнти рецепта «{r['name']}» зі запасів."


def tool_grocy_recipe_shopping(args: dict) -> str:
    r, err = _grocy_recipe_one(args.get("recipe", ""))
    if err:
        return err
    f = grocy_client.recipe_fulfillment(r["id"])
    if f.get("need_fulfilled"):
        return f"НЕ ЗМІНЕНО. Для «{r['name']}» усе вже є в запасах."
    grocy_client.recipe_shop_missing(r["id"])
    return f"Додано до списку покупок нестачу для рецепта «{r['name']}» ({f.get('missing_products_count', '?')} інгр.)."


_UNIT_TO_BASE = {"г": ("м", 1), "g": ("м", 1), "кг": ("м", 1000), "kg": ("м", 1000),
                 "мл": ("о", 1), "ml": ("о", 1), "л": ("о", 1000), "l": ("о", 1000)}


def _convert(amount: float, from_unit: str, to_unit: str) -> float | None:
    a, b = from_unit.lower().strip(), to_unit.lower().strip()
    if a == b:
        return amount
    if a in _UNIT_TO_BASE and b in _UNIT_TO_BASE and _UNIT_TO_BASE[a][0] == _UNIT_TO_BASE[b][0]:
        return amount * _UNIT_TO_BASE[a][1] / _UNIT_TO_BASE[b][1]
    return None  # шт vs г and other mismatches: never guess a conversion


def _tandoor_recipe(name: str) -> tuple[dict | None, str | None]:
    pts, _ = qdrant_client().scroll("tandoor_recipes", limit=200, with_payload=True)
    recs = [p.payload for p in pts]
    q = name.lower().strip()
    exact = [r for r in recs if r["title"].lower() == q]
    found = exact or [r for r in recs if all(any(w.startswith(qt) for w in r["title"].lower().split()) for qt in q.split())]
    if not found:
        return None, f"У Tandoor немає рецепта «{name}»."
    if len(found) > 1 and not exact:
        return None, "Уточни, який саме рецепт: " + "; ".join(r["title"] for r in found[:6]) + "."
    return found[0], None


def _extract_ingredients(text: str) -> tuple[int, list[dict]]:
    key = _openai_key()
    if not key or guardrails.budget_left() <= 0:
        raise RuntimeError("немає ключа OpenAI або вичерпано денний ліміт токенів")
    body = {
        "model": os.environ.get("CHAT_MODEL", "gpt-4.1-mini"),
        "response_format": {"type": "json_object"},
        # No "temperature": 0 — gpt-5.6-luna (ADR-0037) rejects any value but the
        # default (1); this call used to only ever run against gpt-4.1-mini.
        "messages": [
            {"role": "system", "content": (
                "З рецепта витягни ВСІ інгредієнти. Відповідь — JSON "
                '{"servings": число, "ingredients": [{"name": "...", "amount": число або null, "unit": "г|кг|мл|л|шт|"}]}. '
                "name — коротка назва продукту українською в називному відмінку однини (напр. «сіль», «кунжут»). "
                "Не вигадуй кількостей: якщо в тексті немає — amount null. Без води. Кожен продукт один раз (склади кількості)."
            )},
            {"role": "user", "content": guardrails.redact(text)},
        ],
    }
    r = requests.post("https://api.openai.com/v1/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    guardrails.record_usage(data.get("usage", {}).get("total_tokens", 0))
    parsed = json.loads(data["choices"][0]["message"]["content"])
    return int(parsed.get("servings") or 1), [i for i in parsed.get("ingredients", []) if i.get("name")]


def _build_draft(title: str, text: str) -> dict:
    servings, items = _extract_ingredients(text)
    units = _grocy_units()
    lines = []
    for it in items:
        amount, unit = it.get("amount"), (it.get("unit") or "").strip()
        found = _grocy_match(it["name"], strict=True)
        line = {"name": it["name"], "amount": amount, "unit": unit, "product": None, "qty": None, "note": ""}
        if found:
            p = found[0]
            line["product"] = p
            stock_unit = units.get(p["qu_id_stock"], "")
            if not amount:
                line["note"] = "кількість не вказана — пропущено"
            else:
                qty = _convert(float(amount), unit or stock_unit, stock_unit)
                if qty is None:
                    line["note"] = f"одиниці не сходяться ({_fmt_amount(amount)} {unit} проти {stock_unit}) — пропущено"
                else:
                    line["qty"] = qty
        else:
            line["note"] = "нового продукту в Grocy немає — створю без запасу" if amount else "продукту в Grocy немає, кількості немає — пропущено"
        lines.append(line)
    return {"title": title, "servings": servings, "lines": lines}


def _draft_text(d: dict) -> str:
    out = [f"Чернетка рецепта «{d['title']}» для Grocy ({d['servings']} порц.):"]
    for l in d["lines"]:
        amt = f"{_fmt_amount(l['amount'])} {l['unit']}".strip() if l["amount"] else "?"
        prod = f" → {l['product']['name']}" if l["product"] else ""
        note = f" ({l['note']})" if l["note"] else ""
        out.append(f"- {l['name']}: {amt}{prod}{note}")
    out.append("Записати в Grocy? Відповідь «так» — запишу; або скажіть, що виправити.")
    return "\n".join(out)


def _grocy_title_match(query: str, title: str) -> bool:
    q, tl = query.lower().split(), title.lower().split()
    return all(any(w.startswith(qt) for w in tl) for qt in q)


def tool_grocy_recipe_import(args: dict, user_text: str = "") -> str:
    name = (args.get("recipe") or "").strip()
    d = _rs()["draft"]
    # A write needs a fresh draft for this very recipe AND a real "так" from
    # the user; anything else (a new dish name sent with confirm=true) is just
    # a request for a draft, never a refusal or a silent write.
    if (args.get("confirm") and _fresh_recipe_draft() and d and _CONFIRM_RE.search(user_text)
            and (not name or _grocy_title_match(name, d["title"]))):
        return _write_draft(d)
    _rs()["ctx_at"] = time.time()
    rec, err = _tandoor_recipe(name)
    if err:
        return "НЕ ЗМІНЕНО. " + err
    if any(r["name"].lower() == rec["title"].lower() for r in grocy_client.objects("recipes")):
        return f"НЕ ЗМІНЕНО. Рецепт «{rec['title']}» уже є в Grocy."
    d = _build_draft(rec["title"], rec["text"])
    if not any(l["amount"] for l in d["lines"]):
        return f"НЕ ЗМІНЕНО. У тексті «{rec['title']}» немає інгредієнтів з кількостями — переносити нічого."
    # Tandoor's own step-by-step text (already has "Крок N: ..." markers from
    # textify) — kept as the Grocy recipe's description so recipe_cook can
    # walk through it later, instead of being discarded after the draft.
    d["description"] = rec["text"]
    _rs().update(draft=d, at=time.time())
    return _draft_text(d)


def _write_draft(d: dict) -> str:
    units = {u["name"].lower(): u["id"] for u in grocy_client.objects("quantity_units")}
    loc = next((l["id"] for l in grocy_client.objects("locations") if l["name"] == "Кухня"), 1)
    grp = next((g["id"] for g in grocy_client.objects("product_groups") if g["name"] == "Продукти"), None)
    rid = grocy_client.create("recipes", {"name": d["title"], "type": "normal", "base_servings": d["servings"],
                                          "description": d.get("description") or "Створено голосом через Velesha"})
    written, created = 0, 0
    for l in d["lines"]:
        if l["product"] is None and l["amount"]:
            unit_id = units.get(l["unit"].lower()) or units.get("шт") or units.get("piece")
            data = {"name": l["name"], "location_id": loc, "qu_id_stock": unit_id, "qu_id_purchase": unit_id,
                    "qu_id_consume": unit_id, "qu_id_price": unit_id, "description": "Створено при імпорті рецепта"}
            if grp:
                data["product_group_id"] = grp
            pid = grocy_client.create("products", data)
            l["product"] = {"id": pid, "qu_id_stock": unit_id}
            l["qty"] = float(l["amount"])
            created += 1
        if l["product"] is not None and l["qty"]:
            grocy_client.create("recipes_pos", {"recipe_id": rid, "product_id": l["product"]["id"],
                                                "amount": l["qty"], "qu_id": l["product"]["qu_id_stock"]})
            written += 1
    _rs().update(draft=None)
    return f"Створено рецепт «{d['title']}» у Grocy: {written} інгредієнтів, нових продуктів {created}."


def _recipe_add_schema() -> dict:
    return {"type": "function", "function": {
        "name": "recipe_add",
        "description": (
            "Створити НОВИЙ рецепт напряму в Grocy — власний, продиктований користувачем, або знайдений в "
            "інтернеті (web_search). Використовуй, коли рецепта ще нема ні в Grocy, ні в Tandoor (для тих, що "
            "вже є в Tandoor, є grocy_recipe_import). Спершу без confirm — повертає чернетку на підтвердження. "
            "confirm=true лише коли користувач явно підтвердив чернетку."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Назва рецепта"},
                "ingredients": {"type": "array", "items": {"type": "string"},
                                "description": "Інгредієнти з кількостями, кожен рядком (напр. «200 г цукру»)"},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "Кроки приготування, кожен рядком"},
                "confirm": {"type": "boolean", "description": "true — записати підтверджену чернетку"},
            },
            "required": ["title", "ingredients"],
        },
    }}


def tool_recipe_add(args: dict, user_text: str = "") -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return "НЕ ЗМІНЕНО. Яка назва рецепта?"
    _rs()["ctx_at"] = time.time()  # same 60-min "recipe conversation" window as grocy_recipe_import (ADR-0033)
    d = _rs()["draft"]
    if (args.get("confirm") and _fresh_recipe_draft() and d and _CONFIRM_RE.search(user_text)
            and _grocy_title_match(title, d["title"])):
        return _write_draft(d)
    if any(r["name"].lower() == title.lower() for r in grocy_client.objects("recipes")):
        return f"НЕ ЗМІНЕНО. Рецепт «{title}» уже є в Grocy."
    ingredients = [i.strip() for i in (args.get("ingredients") or []) if i and i.strip()]
    if not ingredients:
        return "НЕ ЗМІНЕНО. Продиктуй інгредієнти з кількостями."
    steps = [s.strip() for s in (args.get("steps") or []) if s and s.strip()]
    text = "; ".join(ingredients)
    if steps:
        text += "\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    d = _build_draft(title, text)
    if not any(l["amount"] for l in d["lines"]):
        return "НЕ ЗМІНЕНО. Не вдалось розпізнати кількості в інгредієнтах — продиктуй ще раз, напр. «200 г цукру»."
    # Steps kept verbatim (not re-derived from the LLM ingredient-extraction
    # pass) so recipe_cook can walk through exactly what the user dictated.
    d["description"] = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else ""
    _rs().update(draft=d, at=time.time())
    return _draft_text(d)


# Splits a Grocy recipe's description back into steps. Covers both
# conventions this file writes: "1. text" (recipe_add) and Tandoor's own
# "Крок N: ..." (grocy_recipe_import, via textify).
_STEP_SPLIT_RE = re.compile(r"(?:\n|^)\s*(?:\d+\.|Крок\s*\d+:)\s*")
_COOK_TTL = 60 * 60  # a real cooking session can run long — same window as _fresh_import_ctx
_cook_state: dict[str, dict] = {}


def _cook() -> dict:
    return _cook_state.setdefault(users.current(), {"recipe": None, "steps": [], "idx": 0, "at": 0.0})


def _fresh_cook() -> bool:
    c = _cook()
    return bool(c["steps"]) and time.time() - c["at"] < _COOK_TTL


def _split_steps(description: str) -> list[str]:
    parts = [p.strip() for p in _STEP_SPLIT_RE.split(description or "") if p.strip()]
    return parts or ([description.strip()] if (description or "").strip() else [])


def _recipe_cook_schema() -> dict:
    return {"type": "function", "function": {
        "name": "recipe_cook",
        "description": (
            "Покроково провести користувача голосом по рецепту з Grocy. action='start' — почати готування "
            "(потрібна name); 'next' — наступний крок; 'repeat' — повторити поточний крок ще раз; "
            "'restart' — почати цей рецепт заново з кроку 1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "next", "repeat", "restart"]},
                "name": {"type": "string", "description": "Назва рецепта — лише для action='start'"},
            },
            "required": ["action"],
        },
    }}


def tool_recipe_cook(args: dict) -> str:
    action = args.get("action")
    if action == "start":
        name = (args.get("name") or "").strip()
        if not name:
            return "НЕ ЗМІНЕНО. Який рецепт готуємо?"
        found = [r for r in grocy_client.objects("recipes")
                 if name.lower() in r["name"].lower() or r["name"].lower() in name.lower()]
        if not found:
            return f"НЕ ЗМІНЕНО. У Grocy немає рецепта «{name}»."
        if len(found) > 1:
            return "Уточни, який саме рецепт: " + "; ".join(r["name"] for r in found[:5]) + "."
        recipe = found[0]
        steps = _split_steps(recipe.get("description") or "")
        if not steps:
            return f"У рецепта «{recipe['name']}» немає збережених кроків приготування."
        _cook_state[users.current()] = {"recipe": recipe["name"], "steps": steps, "idx": 0, "at": time.time()}
        return f"Починаємо «{recipe['name']}» ({len(steps)} крок(ів)). Крок 1: {steps[0]}"
    if not _fresh_cook():
        return "НЕ ЗМІНЕНО. Немає активного рецепта — скажи, який рецепт почати готувати."
    c = _cook()
    c["at"] = time.time()
    if action == "repeat":
        return f"Крок {c['idx'] + 1} з {len(c['steps'])}: {c['steps'][c['idx']]}"
    if action == "restart":
        c["idx"] = 0
        return f"Починаємо «{c['recipe']}» заново. Крок 1: {c['steps'][0]}"
    if action == "next":
        if c["idx"] + 1 >= len(c["steps"]):
            return f"Це був останній крок. «{c['recipe']}» готовий!"
        c["idx"] += 1
        return f"Крок {c['idx'] + 1} з {len(c['steps'])}: {c['steps'][c['idx']]}"
    return "НЕ ЗМІНЕНО. Не зрозумів дію — start, next, repeat чи restart?"


_NOTES_DIR = pathlib.Path(__file__).parent / "data" / "notes"
_NOTE_ADD_RE = re.compile(r"(запиши|запам'ятай|запамятай|занотуй|додай нотатку|нова нотатка|збережи нотатку)", re.IGNORECASE)
_NOTES_SIM_THRESHOLD = 0.5  # bge-m3 cosine, tuned loosely — see ADR-0046


def _notes_file() -> pathlib.Path:
    return _NOTES_DIR / f"{users.current()}.jsonl"  # the caller only ever sees their own file


def _note_add_schema() -> dict:
    return {"type": "function", "function": {
        "name": "note_add",
        "description": "Записати особисту нотатку користувача (бачить лише він). Лише за явним проханням записати/запам'ятати.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "Текст нотатки, як сказав користувач"}}, "required": ["text"]}}}


def _notes_search_schema() -> dict:
    return {"type": "function", "function": {
        "name": "notes_search",
        "description": "Особисті нотатки користувача: пошук за словом/змістом (напр. «де батарейки») або останні, якщо без запиту. Інших людей нотатки недоступні.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Що шукати; порожньо — останні"}}}}}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def tool_note_add(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "НЕ ЗМІНЕНО. Порожня нотатка."
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"), "text": text}
    try:
        row["vec"] = [round(x, 5) for x in embed(text)]
    except Exception:
        pass  # embedding server down — note is still saved, just substring-searchable only until re-embedded
    with _notes_file().open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return f"Записано в особисті нотатки: «{text[:80]}»."


def tool_notes_search(args: dict) -> str:
    path = _notes_file()
    if not path.exists():
        return "Особистих нотаток ще немає."
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    q = (args.get("query") or "").strip()
    if not q:
        picked = rows[-15:]
    else:
        ql = q.lower()
        substr = [r for r in rows if any(w[:5] in r["text"].lower() for w in ql.split() if len(w) >= 3)]
        semantic = []
        try:
            qvec = embed(q)
            scored = sorted(
                ((r, _cosine(qvec, r["vec"])) for r in rows if r.get("vec")),
                key=lambda pair: pair[1], reverse=True)
            semantic = [r for r, score in scored[:5] if score >= _NOTES_SIM_THRESHOLD]
        except Exception:
            pass  # embedding server down — substring hits still work
        seen, picked = set(), []
        for r in substr + semantic:
            key = (r["at"], r["text"])
            if key not in seen:
                seen.add(key)
                picked.append(r)
    if not picked:
        return "У ваших нотатках нічого не знайдено."
    return "\n".join(f"{r['at']}: {r['text']}" for r in picked[:15])








# Only devices named here can be switched by the agent: the same HA has the
# fridge, oven and kettle as switches too (ADR-0039). Extend via HA_CONTROL_ALLOW.
def _control_allowed() -> dict[str, str]:
    ids = [e.strip() for e in os.environ.get("HA_CONTROL_ALLOW", "switch.tv,light.2,light.1_2").split(",") if e.strip()]
    names = {}
    for st in ha_client.get_states():
        if st["entity_id"] in ids:
            names[st["entity_id"]] = st.get("attributes", {}).get("friendly_name") or st["entity_id"]
    return names


_STOPWORDS = {"в", "у", "на", "до", "з", "із", "та", "і", "й", "мій", "моя", "будь", "ласка"}
_DOMAIN_SYNONYMS = {"light": ["світло", "лампа", "лампочка"], "switch": ["розетка", "вимикач"]}


def _match_allowed(hint: str, allowed: dict[str, str]) -> list[str]:
    """Every meaningful word of the phrase must match the device (name or domain synonym,
    inflection-tolerant) — a lone generic word like "світло" must not match every lamp.
    Several lamps sharing a room word (e.g. two "зала" lamps) all match: "світло в залі"
    means the room's light, not one specific bulb."""
    tokens = [w for w in hint.lower().split() if w not in _STOPWORDS]
    if not tokens:
        return []
    matches = []
    for eid, name in allowed.items():
        words = name.lower().replace("_", " ").split() + _DOMAIN_SYNONYMS.get(eid.split(".")[0], [])
        def hit(qt: str) -> bool:
            return any(w.startswith(qt[:4]) or qt.startswith(w[:4]) or difflib.SequenceMatcher(None, qt, w).ratio() > 0.72
                       for w in words)
        if all(hit(qt) for qt in tokens):
            matches.append(eid)
    return matches


def _timer_for(eid: str) -> str | None:
    """HA timer helper that switches this device off: HA_TIMER_MAP="switch.tv=timer.tv_off" (ADR-0041)."""
    pairs = os.environ.get("HA_TIMER_MAP", "switch.tv=timer.tv_off").split(",")
    return dict(p.strip().split("=", 1) for p in pairs if "=" in p).get(eid)


def _kodi_remaining_seconds() -> tuple[int, bool, str] | None:
    """(seconds left, paused, title) of what plays on Kodi now, else None."""
    for sess in jellyfin_client.sessions():
        label = f"{sess.get('DeviceName', '')} {sess.get('Client', '')}".lower()
        item, st = sess.get("NowPlayingItem"), sess.get("PlayState") or {}
        if "kodi" in label and item and item.get("RunTimeTicks") and st.get("PositionTicks") is not None:
            return (max(item["RunTimeTicks"] - st["PositionTicks"], 0) // 10_000_000, bool(st.get("IsPaused")), item.get("Name", "?"))
    return None


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _timer_status(timer_id: str, name: str) -> str:
    st = ha_client.get_state(timer_id)
    if st["state"] == "idle":
        return f"Таймер вимкнення «{name}» не запущено."
    fin = st["attributes"].get("finishes_at")
    when = datetime.datetime.fromisoformat(fin).astimezone(_TZ) if fin else None
    left = f", вимкну о {when:%H:%M}" if when else ""
    return f"Таймер вимкнення «{name}» {'на паузі' if st['state'] == 'paused' else 'іде'}{left}."


def tool_ha_switch(args: dict, user_text: str = "") -> str:
    allowed = _control_allowed()
    hint, state, when = (args.get("device") or "").strip(), args.get("state"), args.get("when") or "now"
    eids = _match_allowed(hint, allowed)
    if not eids:
        return (f"НЕ ЗМІНЕНО. «{hint}» немає в списку пристроїв, якими мені дозволено керувати"
                + (f" (дозволено: {', '.join(allowed.values())})." if allowed else "."))
    if when != "now" and len(eids) > 1:
        return "НЕ ЗМІНЕНО. Це стосується кількох пристроїв (" + ", ".join(allowed[e] for e in eids) + "), а таймер можна поставити лише на один. Уточни, який саме."
    eid = eids[0]
    name = allowed[eid]
    # The tool is offered on timer words too, so re-check what the user actually asked for:
    # switching (now / scheduling) needs a power verb or a timer word; status and cancel need a timer word.
    if user_text:
        need = _TIMER_RE if when in ("status", "cancel") else None
        ok = bool(_TIMER_RE.search(user_text)) if need else bool(_POWER_RE.search(user_text) or _TIMER_RE.search(user_text))
        if when == "now" and not _POWER_RE.search(user_text):
            ok = False
        if not ok:
            return "НЕ ЗМІНЕНО. Це не схоже на пряме прохання; уточни, що зробити."
    if when in ("after_playback", "in_minutes", "status", "cancel"):
        timer = _timer_for(eid)
        if not timer:
            return f"НЕ ЗМІНЕНО. Для «{name}» не налаштовано таймер вимкнення (HA_TIMER_MAP)."
        if when == "status":
            return _timer_status(timer, name)
        if when == "cancel":
            ha_client.call_service("timer", "cancel", timer)
            return f"Скасовано: таймер вимкнення «{name}»."
        if state == "on":
            return "НЕ ЗМІНЕНО. Відкладати можна лише вимкнення."
        if when == "after_playback":
            info = _kodi_remaining_seconds()
            if not info:
                return "НЕ ЗМІНЕНО. Зараз на Kodi нічого не грає, тож нема чого чекати. Вимкнути одразу?"
            seconds, paused, title = info
            seconds += 60  # position reports lag by seconds; do not cut the last scene
            note = f" Фільм зараз на паузі: таймер не чекатиме, перепостав, коли продовжиш." if paused else ""
            what = f"коли закінчиться «{title}»"
        else:
            minutes = args.get("minutes")
            if not isinstance(minutes, int) or not 1 <= minutes <= 720:
                return "НЕ ЗМІНЕНО. Вкажи, через скільки хвилин (1–720)."
            seconds, note, what = minutes * 60, "", f"через {minutes} хв"
        ha_client.call_service("timer", "start", timer, duration=_hms(seconds))
        st = ha_client.get_state(timer)
        if st["state"] != "active":
            return f"НЕ ЗМІНЕНО. Таймер «{name}» не запустився."
        fin = st["attributes"].get("finishes_at")  # HA's own clock: the timer runs there, our machine's clock may drift
        end = datetime.datetime.fromisoformat(fin).astimezone(_TZ) if fin else datetime.datetime.now(_TZ) + datetime.timedelta(seconds=seconds)
        return f"Заплановано: вимкну «{name}» {what}, близько {end:%H:%M}.{note}"
    if state not in ("on", "off"):
        return "НЕ ЗМІНЕНО. Вкажи, увімкнути чи вимкнути."
    ok, failed = [], []
    for e in eids:
        ha_client.call_service(e.split(".")[0], "turn_on" if state == "on" else "turn_off", e)
    for e in eids:
        for _ in range(6):  # the state follows the command a moment later
            time.sleep(0.5)
            if ha_client.get_state(e)["state"] == state:
                ok.append(allowed[e]); break
        else:
            failed.append(allowed[e])
    verb = "Увімкнено" if state == "on" else "Вимкнено"
    text = f"{verb}: {', '.join(ok)}." if ok else ""
    if failed:
        text += f" НЕ ЗМІНЕНО для: {', '.join(failed)} (команду надіслано, стан не підтверджено)."
    return text.strip()


VACUUM_ENTITY = os.environ.get("VACUUM_ENTITY", "vacuum.roborock_s7")
_VACUUM_SERVICE = {"start": "start", "stop": "stop", "pause": "pause", "dock": "return_to_base", "locate": "locate"}
_VACUUM_OK_TEXT = {
    "start": "Запущено прибирання.",
    "stop": "Зупинено прибирання.",
    "pause": "Поставлено на паузу.",
    "dock": "Запущено повернення на базу.",
    "locate": "Увімкнено сигнал (пікає), щоб знайти робота.",
}


VACUUM_MODE_ENTITY = os.environ.get("VACUUM_MODE_ENTITY", "select.spalnia_roborock_s7_cleaning_mode")
_area_registry_cache: dict[str, list[dict]] = {}
_AREA_CACHE_TTL = 10 * 60


def _resolve_area(name: str) -> tuple[str | None, str | None]:
    """(area_id, None) on a clear match, (None, error) otherwise — never guess
    between ambiguous rooms (this household has both "Зала" and "Вітальня
    Дніпро", both loosely matching "вітальня")."""
    now = time.time()
    if not _area_registry_cache or now - _area_registry_cache.get("at", 0) > _AREA_CACHE_TTL:
        try:
            _area_registry_cache["areas"] = ha_client.area_registry()
            _area_registry_cache["at"] = now
        except Exception as e:
            print(f"area registry fetch error: {e}", flush=True)
            return None, f"Не вдалось перевірити список кімнат: {e}"
    name_l = name.lower().strip()
    exact, fuzzy = [], []
    for area in _area_registry_cache.get("areas", []):
        candidates = [area["name"].lower()] + [a.lower() for a in area.get("aliases", []) if a]
        if name_l in candidates:
            exact.append(area)
        elif any(name_l in c or c in name_l for c in candidates):
            fuzzy.append(area)
    matches = exact or fuzzy
    if not matches:
        return None, f"Не знайшов кімнату «{name}» в Home Assistant."
    if len(matches) > 1:
        return None, "Кілька кімнат підходять: " + "; ".join(a["name"] for a in matches[:5]) + ". Уточни точніше."
    return matches[0]["area_id"], None


def _do_vacuum_action(action: str, area_id: str | None = None, mode: str | None = None) -> str:
    service = _VACUUM_SERVICE.get(action)
    if not service:
        return "НЕ ЗМІНЕНО. Не зрозумів дію — start, stop, pause, dock чи locate?"
    try:
        if mode:
            ha_client.call_service("select", "select_option", VACUUM_MODE_ENTITY, option=mode)
        if action == "start" and area_id:
            ha_client.call_service_data("vacuum", "clean_area", VACUUM_ENTITY, cleaning_area_id=[area_id])
        else:
            ha_client.call_service("vacuum", service, VACUUM_ENTITY)
    except Exception as e:
        return f"НЕ ЗМІНЕНО. Помилка керування роботом: {e}"
    return _VACUUM_OK_TEXT[action]


def _vacuum_schema() -> dict:
    return {"type": "function", "function": {
        "name": "vacuum_control",
        "description": (
            "Керування роботом-пилососом ЗАРАЗ. start/stop/pause/dock (на базу заряджатись)/locate (пікнути, щоб "
            "знайти). room — прибрати конкретну кімнату замість усієї квартири (лише з action=start). "
            "mode — vacuum (без води)/mop (лише швабра)/vac_and_mop. Для запуску НА ПІЗНІШЕ — vacuum_schedule. "
            "Для стану/заряду/чи прибирає зараз — get_live_state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "pause", "dock", "locate"]},
                "room": {"type": "string", "description": "Кімната (напр. 'кухня'); порожньо = вся квартира"},
                "mode": {"type": "string", "enum": ["vacuum", "mop", "vac_and_mop"],
                         "description": "vacuum — прибирання без води. Лише якщо користувач явно просить."},
            },
            "required": ["action"],
        },
    }}


def tool_vacuum_control(args: dict) -> str:
    action = args.get("action")
    room = (args.get("room") or "").strip()
    area_id = None
    if room:
        area_id, err = _resolve_area(room)
        if err:
            return "НЕ ЗМІНЕНО. " + err
    result = _do_vacuum_action(action, area_id, args.get("mode"))
    return f"{result} ({room})" if room and not result.startswith("НЕ ЗМІНЕНО") else result


def _vacuum_schedule_schema() -> dict:
    return {"type": "function", "function": {
        "name": "vacuum_schedule",
        "description": (
            "Запланувати запуск робота-пилососа на потрібний час (time+date або in_minutes) — усю квартиру "
            "або конкретну кімнату (room), з режимом (mode: vacuum — без води, mop, vac_and_mop). "
            "Для запуску ЗАРАЗ — vacuum_control, не цей інструмент."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "Година, напр. '19' чи '8:00'"},
                "date": {"type": "string", "description": "'сьогодні'/'завтра'/конкретна дата; порожньо = сьогодні"},
                "in_minutes": {"type": "integer", "description": "Альтернатива time/date — через скільки хвилин"},
                "room": {"type": "string", "description": "Кімната (напр. 'кухня'); порожньо = вся квартира"},
                "mode": {"type": "string", "enum": ["vacuum", "mop", "vac_and_mop"], "description": "vacuum — без води"},
            },
            "required": [],
        },
    }}


def tool_vacuum_schedule(args: dict) -> str:
    now = datetime.datetime.now(_TZ)
    when, err = _resolve_when(args, now)
    if err:
        return "НЕ ЗМІНЕНО. " + err
    room = (args.get("room") or "").strip()
    area_id = None
    if room:
        area_id, err = _resolve_area(room)
        if err:
            return "НЕ ЗМІНЕНО. " + err
    mode = args.get("mode")
    if mode and mode not in ("vacuum", "mop", "vac_and_mop"):
        return "НЕ ЗМІНЕНО. Режим має бути vacuum, mop або vac_and_mop."
    label = "Пилосос: " + (room or "вся квартира") + (f", {mode}" if mode else "")
    calendar, _ = _reminder_calendar_for(users.current())
    ha_client.call_service_data("calendar", "create_event", calendar, summary=label,
        description="VACUUM:" + json.dumps({"room": area_id, "mode": mode}),
        start_date_time=when.strftime("%Y-%m-%d %H:%M:%S"),
        end_date_time=(when + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))
    if not _reminder_confirmed(calendar, label, when):
        return "НЕ ЗМІНЕНО. Команду надіслано, але подія не підтвердилась у календарі."
    return f"Заплановано: {label} {when:%d.%m о %H:%M}."


def _fmt_dur(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60} год {m % 60} хв" if m >= 60 else f"{m} хв"


def _series(entity_id: str, start, end) -> list[tuple]:
    out = []
    for h in ha_client.get_history(entity_id, start.isoformat(), end.isoformat()):
        if h.get("state") in (None, "unknown", "unavailable"):
            continue
        out.append((max(datetime.datetime.fromisoformat(h["last_changed"]).astimezone(_TZ), start), h["state"]))
    return out


def _value_at(series: list[tuple], t) -> float | None:
    val = None
    for ts, v in series:
        if ts > t:
            break
        val = _num(v)
    return val if val is not None else (_num(series[0][1]) if series else None)


def tool_get_activity_periods(args: dict) -> str:
    """Cycles of activity (power above a threshold, or state 'on') with durations, computed in code (ADR-0040)."""
    hint = args.get("entity_name", "")
    entity = resolve_entity(hint, unit="W") or resolve_entity(hint)
    if not entity:
        return f"Не знайдено пристрій '{hint}'."
    name = entity["attributes"].get("friendly_name")
    now = datetime.datetime.now(_TZ)
    raw = (args.get("start_date") or "").strip().lower()
    if not raw or raw in _WEEK_WORDS:
        first_day = now.date() - datetime.timedelta(days=10)
    else:
        first_day = _parse_day(raw)
        if not first_day:
            return f"Не вдалось розпізнати дату '{args.get('start_date')}'."
    last_day = _parse_day(args["end_date"]) if args.get("end_date") else None
    start = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=_TZ)
    end = min(datetime.datetime.combine(last_day, datetime.time.min, tzinfo=_TZ) + datetime.timedelta(days=1), now) if last_day else now
    series = _series(entity["entity_id"], start, end)
    if not series:
        return f"Немає даних по '{name}' за цей період (HA зберігає історію ~10 днів)."

    numeric = any(_num(v) is not None for _, v in series)
    unit = entity["attributes"].get("unit_of_measurement", "")
    threshold = float(args["threshold"]) if args.get("threshold") is not None else 5.0
    gap = datetime.timedelta(minutes=int(args["merge_gap_minutes"]) if args.get("merge_gap_minutes") is not None else (10 if numeric else 2))
    active = (lambda v: (_num(v) or 0) > threshold) if numeric else (lambda v: v == "on")

    periods = []  # [start, end]
    for i, (t, v) in enumerate(series):
        nxt = series[i + 1][0] if i + 1 < len(series) else end
        if active(v):
            if periods and t - periods[-1][1] <= gap:
                periods[-1][1] = nxt
            else:
                periods.append([t, nxt])
    if not periods:
        return f"«{name}» за цей період не було активним (поріг {_fmt(threshold) + ' ' + unit if numeric else 'увімкнено'})."

    energy = resolve_entity(hint, unit="kWh") if numeric else None
    eseries = _series(energy["entity_id"], start, end) if energy else []
    ongoing = active(series[-1][1]) and periods[-1][1] >= end - datetime.timedelta(seconds=1)

    def row(p) -> str:
        minutes = (p[1] - p[0]).total_seconds() / 60
        line = f"{p[0]:%d.%m %H:%M}–{p[1]:%H:%M}, {_fmt_dur(minutes)}"
        if eseries:
            a, b = _value_at(eseries, p[0]), _value_at(eseries, p[1])
            if a is not None and b is not None and b >= a:
                line += f", {_fmt(b - a)} кВт·год"
        return line

    shown = periods[-10:]
    durs = [(p[1] - p[0]).total_seconds() / 60 for p in periods]
    lines = [row(p) + (" (триває зараз)" if ongoing and p is periods[-1] else "") for p in shown]
    rule = f"потужність > {_fmt(threshold)} {unit}" if numeric else "увімкнено"
    head = (f"{name}: {len(periods)} цикл(ів) ({rule}, паузи до {int(gap.total_seconds() // 60)} хв склеєно), "
            f"середня тривалість {_fmt_dur(sum(durs) / len(durs))}, найдовший {_fmt_dur(max(durs))}, найкоротший {_fmt_dur(min(durs))}.")
    last = f"Останній: {row(periods[-1])}" + (" (триває зараз)" if ongoing else "") + "."
    more = f" Показано останні {len(shown)} з {len(periods)}." if len(periods) > len(shown) else ""
    return head + "\n" + last + more + "\n" + "\n".join(lines)


def _get_watched_movies_schema() -> dict:
    return next(t for t in TOOLS if t["function"]["name"] == "get_watched_movies")


def _log_watched_movie_schema() -> dict:
    return next(t for t in TOOLS if t["function"]["name"] == "log_watched_movie")


def _index_movie(row: dict) -> None:
    """Embed and upsert right after the write (variant A — simple, low volume)."""
    text = (f"{row['title']} ({row['year'] or '?'}). Режисер: {row['director'] or '?'}. "
            f"Жанри: {', '.join(row['genres'])}. Оцінка: {row['rating']}/10. {row['review'] or ''}")
    vector = embed(text)
    key = row["imdb_id"] or f"{row['title'].lower()}-{row['year']}"
    point_id = abs(hash(key)) % (2**63)
    qdrant_client().upsert("watched_movies", points=[{
        "id": point_id, "vector": vector,
        "payload": {"imdb_id": row["imdb_id"], "title": row["title"], "year": row["year"], "director": row["director"],
                    "genres": row["genres"], "rating": row["rating"], "watched_date": row["watched_date"],
                    "text": text},
    }])
    movies_db.mark_indexed(row["id"])


def tool_log_watched_movie(args: dict) -> str:
    title, year = (args.get("title") or "").strip(), args.get("year")
    rating = args.get("rating")
    if not title or not isinstance(year, int):
        return "НЕ ЗМІНЕНО. Потрібні назва і рік."
    if not isinstance(rating, (int, float)) or not 0 <= rating <= 10:
        return "НЕ ЗМІНЕНО. Оцінка має бути числом від 0 до 10."
    from qdrant_client.http.exceptions import UnexpectedResponse
    if "watched_movies" not in [c.name for c in qdrant_client().get_collections().collections]:
        from qdrant_client.models import Distance, VectorParams
        qdrant_client().create_collection("watched_movies", vectors_config=VectorParams(size=len(embed(title)), distance=Distance.COSINE))
    row = movies_db.log_watched_movie(title, year, (args.get("imdb_id") or "").strip() or None, args.get("director"),
                                      args.get("genres") or [], float(rating), args.get("review"))
    try:
        _index_movie(row)
        indexed = True
    except (requests.exceptions.RequestException, UnexpectedResponse) as e:
        indexed = False
        print(f"movie index error: {type(e).__name__}", flush=True)
    note = "" if indexed else " (запис збережено, індексація для рекомендацій не вдалась — спробую пізніше)"
    return f"Записано: «{title}» ({year}) — {rating}/10.{note}"


def tool_get_watched_movies(args: dict) -> str:
    rows = movies_db.get_watched_movies(min_rating=args.get("min_rating"), genre=args.get("genre"))
    if not rows:
        return "Оцінок переглянутих фільмів ще немає."
    return "\n".join(f"{r['title']} ({r['year'] or '?'}){' — реж. ' + r['director'] if r.get('director') else ''}: {r['rating']}/10" + (f" — {r['review']}" if r["review"] else "") for r in rows)


# One calendar per user (ADR-0044): "default" (single-user mode, ADR-0035) keeps the
# calendar already created by hand; any other user gets one auto-provisioned on first
# use via HA's config-flow API, and the mapping is cached so we never create it twice.
_REMINDER_CAL_FILE = pathlib.Path(__file__).parent / "data" / "reminder_calendars.json"
_REMINDER_CAL_DEFAULT = os.environ.get("REMINDER_CALENDAR", "calendar.nagaduvannia")


def _reminder_calendar_map() -> dict:
    if _REMINDER_CAL_FILE.exists():
        return json.loads(_REMINDER_CAL_FILE.read_text())
    return {}


def _save_reminder_calendar_map(m: dict) -> None:
    _REMINDER_CAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REMINDER_CAL_FILE.write_text(json.dumps(m, ensure_ascii=False))


def _reminder_calendar_for(user: str) -> tuple[str, bool]:
    """(entity_id, just_created) — just_created means the automation for it still needs setting up."""
    if user == "default":
        return _REMINDER_CAL_DEFAULT, False
    if override := os.environ.get(f"REMINDER_CALENDAR_{user.upper()}"):
        return override, False
    cached = _reminder_calendar_map()
    if user in cached:
        return cached[user], False
    title = f"Нагадування {user}"
    states = ha_client.get_states()
    existing = next((s["entity_id"] for s in states
                     if s["entity_id"].startswith("calendar.") and s["attributes"].get("friendly_name") == title), None)
    if existing:  # a previous attempt created the calendar but crashed before caching it — reuse, don't duplicate
        cached[user] = existing
        _save_reminder_calendar_map(cached)
        return existing, True
    before = {s["entity_id"] for s in states}
    r = ha_client._session.post(f"{ha_client.HA_URL}/api/config/config_entries/flow",
                                json={"handler": "local_calendar", "show_advanced_options": False}, timeout=15)
    r.raise_for_status()
    flow = r.json()
    r2 = ha_client._session.post(f"{ha_client.HA_URL}/api/config/config_entries/flow/{flow['flow_id']}",
                                 json={"calendar_name": title}, timeout=15)
    r2.raise_for_status()
    # HA's own transliteration of the title decides the entity_id, not ours to guess —
    # diff the entity list before/after (one retry: registration can lag the config entry by a beat).
    entity = None
    for _ in range(20):
        after = {s["entity_id"] for s in ha_client.get_states()}
        new_ids = after - before
        if new_ids:
            entity = next((e for e in new_ids if e.startswith("calendar.")), None) or new_ids.pop()
            break
        time.sleep(0.5)
    if not entity:
        raise RuntimeError(f"local_calendar created for {user} but its entity_id never appeared")
    cached[user] = entity
    _save_reminder_calendar_map(cached)
    return entity, True


def _remind_me_schema() -> dict:
    return next(t for t in TOOLS if t["function"]["name"] == "remind_me")


def _parse_clock(s: str) -> tuple[int, int] | None:
    m = _CLOCK_RE.match((s or "").strip())
    if not m:
        return None
    h, mm = int(m[1]), int(m[2]) if m[2] else 0
    return (h, mm) if 0 <= h <= 23 and 0 <= mm <= 59 else None


def _reminder_confirmed(calendar: str, summary: str, when: datetime.datetime) -> bool:
    events = ha_client.calendar_events(calendar,
        (when - datetime.timedelta(minutes=2)).isoformat(), (when + datetime.timedelta(minutes=2)).isoformat())
    return any(e.get("summary") == summary for e in events)


def _resolve_when(args: dict, now: datetime.datetime) -> tuple[datetime.datetime | None, str | None]:
    """(when, None) or (None, error) — shared by remind_me and vacuum_schedule."""
    minutes = args.get("in_minutes")
    if minutes is not None:
        if not isinstance(minutes, int) or not 1 <= minutes <= 10080:
            return None, "Вкажи, через скільки хвилин (1–10080)."
        return now + datetime.timedelta(minutes=minutes), None
    hm = _parse_clock(args.get("time", ""))
    if not hm:
        return None, "Не розпізнав час; вкажи годину, напр. '19' або '19:30', або через скільки хвилин."
    date_raw = (args.get("date") or "").strip().lower()
    if date_raw in ("", "сьогодні", "today"):
        day = now.date()
    elif date_raw in ("завтра", "tomorrow"):
        day = now.date() + datetime.timedelta(days=1)
    else:
        day = _parse_day(date_raw)
        if not day:
            return None, f"Не розпізнав дату '{args.get('date')}'."
    when = datetime.datetime.combine(day, datetime.time(*hm), tzinfo=_TZ)
    if not date_raw and when <= now:  # a bare "о 19" already past today means tomorrow, not silently dropped
        when += datetime.timedelta(days=1)
    return when, None


def tool_remind_me(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "НЕ ЗМІНЕНО. Що нагадати?"
    calendar, _ = _reminder_calendar_for(users.current())
    now = datetime.datetime.now(_TZ)
    when, err = _resolve_when(args, now)
    if err:
        return "НЕ ЗМІНЕНО. " + err
    ha_client.call_service_data("calendar", "create_event", calendar, summary=text,
        start_date_time=when.strftime("%Y-%m-%d %H:%M:%S"), end_date_time=(when + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))
    if not _reminder_confirmed(calendar, text, when):
        return "НЕ ЗМІНЕНО. Команду надіслано, але нагадування не підтвердилось у календарі."
    note = ""
    if not _reminder_notify_target(users.current()):
        note = (f" (адміністратору: постав REMINDER_NOTIFY_{users.current().upper()}=notify.<пристрій>, "
                f"інакше push не прийде — подія в календарі є, доставку веде сам сервер, не автоматизація HA)")
    return f"Заплановано нагадування: «{text}» {when:%d.%m о %H:%M}.{note}"


def _get_reminders_schema() -> dict:
    return next(t for t in TOOLS if t["function"]["name"] == "get_reminders")


def tool_get_reminders(args: dict) -> str:
    calendar, _ = _reminder_calendar_for(users.current())
    now = datetime.datetime.now(_TZ)
    events = ha_client.calendar_events(calendar, now.isoformat(), (now + datetime.timedelta(days=14)).isoformat())
    if not events:
        return "Запланованих нагадувань немає."
    def sort_key(e):
        start = e["start"].get("dateTime") or e["start"].get("date")
        return datetime.datetime.fromisoformat(start).astimezone(_TZ) if "T" in start else now
    events.sort(key=sort_key)
    return "\n".join(_fmt_reminder(e) for e in events)


def _cancel_reminder_schema() -> dict:
    return next(t for t in TOOLS if t["function"]["name"] == "cancel_reminder")


def _fmt_reminder(e: dict) -> str:
    start = e["start"].get("dateTime") or e["start"].get("date")
    dt = datetime.datetime.fromisoformat(start).astimezone(_TZ) if "T" in start else None
    return f"{dt:%d.%m %H:%M} — {e['summary']}" if dt else f"{start} — {e['summary']}"


def tool_cancel_reminder(args: dict, user_text: str = "") -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "НЕ ЗМІНЕНО. Яке нагадування скасувати?"
    if user_text and not re.search(r"скасу|видал|прибери|відмін", user_text, re.IGNORECASE):
        return "НЕ ЗМІНЕНО. Це не схоже на пряме прохання скасувати."
    calendar, _ = _reminder_calendar_for(users.current())
    now = datetime.datetime.now(_TZ)
    events = ha_client.calendar_events(calendar, now.isoformat(), (now + datetime.timedelta(days=14)).isoformat())
    matches = [e for e in events if query.lower() in e["summary"].lower()]
    if not matches:
        return f"НЕ ЗМІНЕНО. Не знайшов активного нагадування зі словом «{query}»."
    if len(matches) > 1:
        return "НЕ ЗМІНЕНО. Кілька збігів: " + "; ".join(_fmt_reminder(e) for e in matches) + ". Уточни точніше."
    e = matches[0]
    if not ha_client.delete_calendar_event(calendar, e["uid"]):
        return "НЕ ЗМІНЕНО. Не вдалось скасувати."
    still = ha_client.calendar_events(calendar, now.isoformat(), (now + datetime.timedelta(days=14)).isoformat())
    if any(x["uid"] == e["uid"] for x in still):
        return "НЕ ЗМІНЕНО. Скасування не підтвердилось."
    return f"Скасовано нагадування: «{e['summary']}»."


def _reminder_notify_target(user: str) -> str | None:
    """Bare notify service name (e.g. "mobile_app_punkas26"), no "notify." prefix."""
    v = os.environ.get(f"REMINDER_NOTIFY_{user.upper()}")
    return v.removeprefix("notify.") if v else None


# Delivery lives here, not in an HA automation on the calendar's own "event:
# start" trigger — that trigger is unreliable for events created shortly
# before they fire (widely reported HA bug, not just a refresh-interval
# thing) and silently missed two real reminders in testing. The calendar
# event is still created (get_reminders reads it, and it's visible in HA's
# own UI) — this poller just also owns pushing the notification, checking
# every _REMINDER_POLL_INTERVAL seconds for events that just started.
_REMINDER_POLL_INTERVAL = 20
# Wider than the poll interval on purpose: a restart (deploy, crash) clears
# _notified_reminders, so this also has to catch anything that started while
# the server was down — a duplicate push after a rare restart is a much
# smaller problem than a reminder that silently never arrives.
_REMINDER_POLL_LOOKBACK = 10 * 60
_notified_reminders: set[str] = set()


async def poll_reminders_once() -> None:
    now = datetime.datetime.now(_TZ)
    window_start = now - datetime.timedelta(seconds=_REMINDER_POLL_LOOKBACK)
    calendars = {"default": _REMINDER_CAL_DEFAULT, **_reminder_calendar_map()}
    for user, calendar in calendars.items():
        try:
            events = ha_client.calendar_events(calendar, window_start.isoformat(), now.isoformat())
        except Exception as e:
            print(f"reminder poll error ({calendar}): {e}", flush=True)
            continue
        for e in events:
            uid = e.get("uid")
            if not uid or uid in _notified_reminders:
                continue
            _notified_reminders.add(uid)
            description = e.get("description") or ""
            if description.startswith("VACUUM:"):
                try:
                    payload = json.loads(description[len("VACUUM:"):])
                    result = await asyncio.to_thread(_do_vacuum_action, "start", payload.get("room"), payload.get("mode"))
                    print(f"vacuum schedule fired ({e['summary']}): {result}", flush=True)
                except Exception as ex:
                    print(f"vacuum schedule error ({calendar}/{uid}): {ex}", flush=True)
                continue
            target = _reminder_notify_target(user)
            if not target:
                continue
            try:
                ha_client.notify(target, "Нагадування", e["summary"])
            except Exception as ex:
                print(f"reminder push error ({calendar}/{uid}): {ex}", flush=True)
    if len(_notified_reminders) > 1000:  # one-off uids, never revisited — cheap cap, not a real cache
        _notified_reminders.clear()


async def reminder_poll_loop() -> None:
    while True:
        try:
            await poll_reminders_once()
        except Exception as e:  # never let one bad tick kill the whole poller
            print(f"reminder poll loop error: {e}", flush=True)
        await asyncio.sleep(_REMINDER_POLL_INTERVAL)


DISPATCH = {
    "search_knowledge": tool_search_knowledge,
    "get_live_state": tool_get_live_state,
    "get_sensor_history": tool_get_sensor_history,
    "qbittorrent_status": tool_qbittorrent_status,
    "qbittorrent_list": tool_qbittorrent_list,
    "qbittorrent_add": tool_qbittorrent_add,
    "toloka_search": tool_toloka_search,
    "ha_switch": tool_ha_switch,
    "vacuum_control": tool_vacuum_control,
    "vacuum_schedule": tool_vacuum_schedule,
    "note_add": tool_note_add,
    "notes_search": tool_notes_search,
    "get_activity_periods": tool_get_activity_periods,
    "log_watched_movie": tool_log_watched_movie,
    "get_watched_movies": tool_get_watched_movies,
    "remind_me": tool_remind_me,
    "cancel_reminder": tool_cancel_reminder,
    "get_reminders": tool_get_reminders,
    "grocy_stock": tool_grocy_stock,
    "grocy_shopping_list": tool_grocy_shopping_list,
    "grocy_recipes": tool_grocy_recipes,
    "grocy_recipe_import": tool_grocy_recipe_import,
    "recipe_add": tool_recipe_add,
    "grocy_recipes_not_imported": tool_grocy_recipes_not_imported,
    "grocy_recipe_consume": tool_grocy_recipe_consume,
    "grocy_recipe_shopping": tool_grocy_recipe_shopping,
    "grocy_consume": tool_grocy_consume,
    "grocy_add_stock": tool_grocy_add_stock,
    "grocy_product_add": tool_grocy_product_add,
    "recipe_cook": tool_recipe_cook,
    "grocy_shopping_add": tool_grocy_shopping_add,
    "toloka_add": tool_toloka_add,
    "web_search": tool_web_search,
}


def call_tool(name: str, arguments_json: str, user_text: str = "") -> str:
    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return "Помилка: невалідні аргументи інструменту."
    handler = DISPATCH.get(name)
    if not handler:
        return f"Невідомий інструмент: {name}"
    if name in ADMIN_TOOLS and not users.is_admin():
        return "НЕ ЗМІНЕНО. Ця дія доступна лише адміністратору."
    try:
        if name in ("qbittorrent_add", "toloka_add", "grocy_recipe_import", "recipe_add", "ha_switch", "cancel_reminder"):
            return handler(args, user_text)
        return handler(args)
    except Exception as e:
        return f"Помилка виконання {name}: {e}"
