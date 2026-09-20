"""Tools the chat model can call — see ADR-0018.

Three tools, chosen to close the exact gaps found in testing:
- search_knowledge: replaces the old always-on blind RAG injection —
  the model now decides when and what to search for.
- get_live_state: live HA state (the door-sensor bug — RAG only ever
  sees an indexed snapshot, never "right now").
- get_sensor_history: period summaries (kWh used, counter change, on/off
  counts and durations) — computed at query time, ADR-0020.
"""

import datetime
import difflib
import json
import os
import re
import time
import zoneinfo

import requests

import guardrails
import grocy_client
import ha_client
import jellyfin_client
import qbit_client
import toloka_client
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
                "(перегляди фільмів/серіалів). НЕ дає живий поточний стан "
                "пристроїв і не рахує суми/дельти — для цього є інші інструменти."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Пошуковий запит"},
                    "source": {
                        "type": "string",
                        "enum": ["ha_history", "jellyfin_library", "tandoor_recipes", "all"],
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
            "name": "play_on_jellyfin_device",
            "description": (
                "ЗАПУСТИТИ відтворення фільму чи серіалу з Jellyfin на пристрої Kodi. "
                "Для серіалу запускає наступний непереглянутий епізод. Викликай ЛИШЕ "
                "коли користувач явно просить увімкнути/запустити/включити фільм чи серіал."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Назва фільму чи серіалу, напр. 'Декстер'"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_jellyfin_playback",
            "description": (
                "Керувати тим, що ЗАРАЗ грає на Kodi: пауза, продовжити, зупинити, "
                "наступна серія, попередня серія. Для запуску нового фільму/серіалу "
                "за назвою є play_on_jellyfin_device."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "stop", "next", "previous"],
                        "description": "pause — пауза, resume — продовжити, stop — зупинити, next/previous — наступна/попередня серія",
                    },
                },
                "required": ["action"],
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
            "name": "jellyfin_find",
            "description": (
                "ТОЧНИЙ пошук за назвою в бібліотеці Jellyfin користувача: чи є там цей фільм/серіал. "
                "Повертає лише справжні збіги назви (не схожі фільми). Викликай першим, коли користувач "
                "просить знайти фільм/серіал або питає, чи він є."
            ),
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "Назва фільму чи серіалу"}},
                "required": ["title"],
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
                "Рецепти, заведені в Grocy, і чи вистачає для них інгредієнтів у запасах. "
                "Для 'що я можу приготувати з наявного', 'чого не вистачає на X'. Це лише рецепти, "
                "яким користувач додав інгредієнти в Grocy; для пошуку рецептів за змістом — search_knowledge."
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
        return datetime.date.fromisoformat(text)
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
CONTROL_TOOLS = {"play_on_jellyfin_device", "control_jellyfin_playback", "qbittorrent_add", "toloka_add",
                 "grocy_consume", "grocy_add_stock", "grocy_shopping_add",
                 "grocy_recipe_consume", "grocy_recipe_shopping"}

# Answered verbatim, without a second model call (ADR-0024).
PASSTHROUGH_TOOLS = {"toloka_search"}
_COMMAND_RE = re.compile(
    r"(включ|увімкн|ввімкн|запуст|постав|відтвор|\bplay\b|пауз|продовж|зупин|стоп|наступн|попередн|далі|пропуст|\bnext\b|\bstop\b|\bpause\b)",
    re.IGNORECASE,
)


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
_GROCY_SHOP_RE = re.compile(r"(список покупок|списку покупок|треба купити|потрібно купити|купити)", re.IGNORECASE)

_GROCY_COOK_RE = re.compile(r"(приготував|приготувала|зварив|зварила|спік|спекла|засмажив|засмажила)", re.IGNORECASE)
_GROCY_RSHOP_RE = re.compile(r"(список покупок|списку покупок|додай.*не вистача|не вистача.*додай)", re.IGNORECASE)
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
    tools = TOOLS if _COMMAND_RE.search(text) else [t for t in TOOLS if t["function"]["name"] not in CONTROL_TOOLS]
    # Adding a torrent needs an actual link in the user's own message —
    # a model can't be allowed to invent one (ADR-0023).
    if _TORRENT_LINK_RE.search(text):
        tools = tools + [_qbit_add_schema()]
    # Read-only and cheap, so always offered: a word gate looked only at the
    # latest message and lost the request in multi-turn talk (ADR-0029).
    tools = tools + [_toloka_search_schema()]
    for name, (desc, gate) in _GROCY_SCHEMAS.items():
        if gate.search(text):  # state-changing: only on an explicit phrase (ADR-0032)
            tools = tools + [_grocy_action_schema(name, desc)]
    for name, (desc, gate) in _GROCY_RECIPE_SCHEMAS.items():
        if gate.search(text):
            tools = tools + [_grocy_recipe_schema(name, desc)]
    if _WEB_RE.search(text) and web_search_available():
        tools = tools + [_web_search_schema()]
    # Adding from Toloka works only on a variant the user was just shown.
    if _fresh_search() and (_ADD_VERB_RE.search(text) or _fresh_pending() or _names_a_category(text)):
        tools = tools + [_toloka_add_schema()]
    return tools


# The only device this tool may ever start playback on — a state-changing
# tool gets an allowlist, not "any session" (ADR-0021).
_PLAY_DEVICE_MARKER = "kodi"
_SESSION_WAIT_SECONDS = 60


def _find_play_session() -> dict | None:
    for sess in jellyfin_client.sessions():
        label = f"{sess.get('DeviceName', '')} {sess.get('Client', '')}".lower()
        if _PLAY_DEVICE_MARKER in label and sess.get("SupportsRemoteControl"):
            return sess
    return None


def _pick_title(term: str) -> dict | None:
    items = jellyfin_client.search(term)
    if not items:
        return None
    term_l = term.lower()
    for item in items:  # prefer a name that starts with the search term
        if item["Name"].lower().startswith(term_l):
            return item
    return items[0]


def tool_play_on_jellyfin_device(args: dict, dry_run: bool = False) -> str:
    title = args.get("title", "").strip()
    if not title:
        return "Не вказано, що запускати."
    item = _pick_title(title)
    if not item:
        return f"У Jellyfin не знайдено '{title}'."

    if item["Type"] == "Series":
        episode = jellyfin_client.next_episode(item["Id"])
        if not episode:
            return f"У серіалі '{item['Name']}' немає епізодів."
        play_id = episode["Id"]
        what = f"{item['Name']}: сезон {episode.get('ParentIndexNumber', '?')} серія {episode.get('IndexNumber', '?')}"
    else:
        play_id, what = item["Id"], item["Name"]

    # Kodi may still be booting after its power outlet was switched on:
    # wait for its Jellyfin session instead of failing at once.
    deadline = time.monotonic() + (0 if dry_run else _SESSION_WAIT_SECONDS)
    while True:
        sess = _find_play_session()
        if sess or time.monotonic() >= deadline:
            break
        time.sleep(3)
    if not sess:
        return "Kodi зараз не в мережі (сесії Jellyfin немає) — спершу увімкни телевізор."

    if dry_run:
        return f"[dry-run] запустив би '{what}' на {sess['DeviceName']}."
    jellyfin_client.play(sess["Id"], play_id)
    return f"Запущено '{what}' на {sess['DeviceName']}."


_PLAYSTATE = {"pause": "Pause", "resume": "Unpause", "stop": "Stop"}


def tool_control_jellyfin_playback(args: dict) -> str:
    action = args.get("action", "")
    sess = _find_play_session()
    if not sess:
        return "Kodi зараз не в мережі (сесії Jellyfin немає)."
    playing = sess.get("NowPlayingItem")
    if not playing:
        return "На Kodi зараз нічого не грає."

    if action in _PLAYSTATE:
        jellyfin_client.playstate(sess["Id"], _PLAYSTATE[action])
        done = {"pause": "Поставлено на паузу", "resume": "Продовжено", "stop": "Зупинено"}[action]
        return f"{done}: {playing.get('SeriesName') or playing.get('Name')}."

    if action in ("next", "previous"):
        if playing.get("Type") != "Episode" or not playing.get("SeriesId"):
            return "Зараз грає не серіал — наступної чи попередньої серії немає."
        step = 1 if action == "next" else -1
        ep = jellyfin_client.adjacent_episode(
            playing["SeriesId"], playing["ParentIndexNumber"], playing["IndexNumber"], step
        )
        if not ep:
            return "Це " + ("остання" if step == 1 else "перша") + " серія серіалу."
        jellyfin_client.play(sess["Id"], ep["Id"])
        return f"Запущено: {playing['SeriesName']}, сезон {ep['ParentIndexNumber']} серія {ep['IndexNumber']} «{ep['Name']}»."

    return f"Невідома дія '{action}'."


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


def tool_jellyfin_find(args: dict) -> str:
    # Semantic search returns the nearest film, never "nothing" — it offered
    # "Ураган (1999)" for "Ідеальний шторм" and the model called it the same
    # film. Jellyfin's own name search only returns real name matches (ADR-0029).
    title = args.get("title", "").strip()
    if not title:
        return "Не вказано назву."
    items = jellyfin_client.search(title)
    if not items:
        return (f"У бібліотеці Jellyfin немає «{title}» (збігів за назвою нема; схожі за змістом фільми "
                "не вважаються збігом).")
    lines = [f"«{i['Name']}» ({i.get('ProductionYear', '?')}, {'серіал' if i['Type'] == 'Series' else 'фільм'})" for i in items[:5]]
    return "У бібліотеці Jellyfin є: " + "; ".join(lines) + "."


def _grocy_units() -> dict[int, str]:
    return {u["id"]: u["name"] for u in grocy_client.objects("quantity_units")}


def _grocy_match(query: str) -> list[dict]:
    """Products best matching a name, best first; only clear matches (score > 0.75)."""
    q = query.lower().strip()
    scored = []
    for p in grocy_client.objects("products"):
        name = p["name"].lower()
        if name == q:
            score = 2.0
        else:  # inflected forms ("цукру" for "Цукор"): best word-vs-word similarity
            score = max((difflib.SequenceMatcher(None, qt, w).ratio()
                         for qt in q.split() for w in name.split()), default=0)
            if any(w.startswith(qt) for qt in q.split() for w in name.split()):
                score = max(score, 1.0)
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
                + ". Рецепти зі структурованими інгредієнтами треба спершу завести в Grocy.")
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


DISPATCH = {
    "search_knowledge": tool_search_knowledge,
    "get_live_state": tool_get_live_state,
    "get_sensor_history": tool_get_sensor_history,
    "play_on_jellyfin_device": tool_play_on_jellyfin_device,
    "control_jellyfin_playback": tool_control_jellyfin_playback,
    "qbittorrent_status": tool_qbittorrent_status,
    "qbittorrent_list": tool_qbittorrent_list,
    "qbittorrent_add": tool_qbittorrent_add,
    "toloka_search": tool_toloka_search,
    "jellyfin_find": tool_jellyfin_find,
    "grocy_stock": tool_grocy_stock,
    "grocy_shopping_list": tool_grocy_shopping_list,
    "grocy_recipes": tool_grocy_recipes,
    "grocy_recipe_consume": tool_grocy_recipe_consume,
    "grocy_recipe_shopping": tool_grocy_recipe_shopping,
    "grocy_consume": tool_grocy_consume,
    "grocy_add_stock": tool_grocy_add_stock,
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
    try:
        if name in ("qbittorrent_add", "toloka_add"):
            return handler(args, user_text)
        return handler(args)
    except Exception as e:
        return f"Помилка виконання {name}: {e}"
