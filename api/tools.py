"""Tools the chat model can call — see ADR-0018.

Three tools, chosen to close the exact gaps found in testing:
- search_knowledge: replaces the old always-on blind RAG injection —
  the model now decides when and what to search for.
- get_live_state: live HA state (the door-sensor bug — RAG only ever
  sees an indexed snapshot, never "right now").
- get_energy_usage: a kWh delta over a date range — not a fact that can
  be pre-textified, has to be computed at query time.
"""

import datetime
import difflib
import json

import ha_client
from qdrant_client import QdrantClient
from search_backend import COLLECTIONS, CANDIDATE_POOL, CHUNKS_PER_COLLECTION, MAX_CHUNK_CHARS, embed, qdrant_client

# Entities whose friendly_name is real but who are pure device-management
# noise for "what's the current state" questions — never a useful match.
_NOISE_SUBSTRINGS = (
    "identifikuvati", "firmware", "child_lock", "power_on_state",
    "backlight_mode", "battery",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Семантичний пошук у базі знань Velesha: рецепти (Tandoor), "
                "історія Home Assistant (проіндексовані події), Jellyfin "
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
                "Поточний (живий, зараз) стан пристрою чи сенсора Home Assistant "
                "за назвою — напр. 'чи двері відчинені', 'яка температура в спальні'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {
                        "type": "string",
                        "description": "Назва пристрою/сенсора українською, напр. 'вхідні двері', 'температура спальня'",
                    }
                },
                "required": ["entity_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_energy_usage",
            "description": "Скільки електроенергії (кВт·год) використав пристрій за вказану дату.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string", "description": "Назва пристрою, напр. 'пралка', 'духовка'"},
                    "date": {"type": "string", "description": "Дата у форматі YYYY-MM-DD"},
                },
                "required": ["entity_name", "date"],
            },
        },
    },
]


def _score(hint: str, friendly_name: str) -> float:
    hint_l, name_l = hint.lower(), friendly_name.lower()
    if hint_l in name_l or name_l in hint_l:
        return 1.0
    return difflib.SequenceMatcher(None, hint_l, name_l).ratio()


# A bare device name ("телевізор") matches several entities equally well
# (the switch itself, plus its Струм/Напруга/Summation delivered
# diagnostic sensors) — prefer the entity you'd actually mean by "стан
# пристрою" over its diagnostics when scores tie. Lower sorts first.
_DOMAIN_PRIORITY = {
    "switch": 0, "light": 0, "climate": 0, "lock": 0, "cover": 0,
    "binary_sensor": 0, "media_player": 0, "person": 0, "alarm_control_panel": 0,
}


def resolve_entity(hint: str, unit: str | None = None) -> dict | None:
    states = ha_client.get_states()
    candidates = []
    for s in states:
        name = s.get("attributes", {}).get("friendly_name")
        if not name:
            continue
        entity_id = s["entity_id"]
        if any(noise in entity_id.lower() for noise in _NOISE_SUBSTRINGS):
            continue
        if unit and s.get("attributes", {}).get("unit_of_measurement") != unit:
            continue
        domain_priority = _DOMAIN_PRIORITY.get(entity_id.split(".", 1)[0], 1)
        candidates.append((_score(hint, name), domain_priority, s))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1]))
    best_score, _, best = candidates[0]
    return best if best_score > 0.3 else None


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
    entity = resolve_entity(args.get("entity_name", ""))
    if not entity:
        return f"Не знайдено пристрій '{args.get('entity_name')}'."
    attrs = entity.get("attributes", {})
    unit = attrs.get("unit_of_measurement", "")
    return (
        f"{attrs.get('friendly_name')} ({entity['entity_id']}): "
        f"{entity.get('state')} {unit}, останнє оновлення {entity.get('last_changed')}"
    )


def tool_get_energy_usage(args: dict) -> str:
    entity = resolve_entity(args.get("entity_name", ""), unit="kWh")
    if not entity:
        return f"Не знайдено лічильник енергії для '{args.get('entity_name')}'."

    date_str = args.get("date", "")
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        return f"Не вдалось розпізнати дату '{date_str}', очікую формат YYYY-MM-DD."

    tz = datetime.timezone(datetime.timedelta(hours=3))  # Europe/Kyiv, no DST handling
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)

    history = ha_client.get_history(entity["entity_id"], start.isoformat(), end.isoformat())
    values = [float(h["state"]) for h in history if h.get("state") not in (None, "unknown", "unavailable")]
    if not values:
        return f"Немає даних по '{entity['attributes'].get('friendly_name')}' за {date_str}."

    delta = max(values) - min(values)
    return f"{entity['attributes'].get('friendly_name')} використав(ла) {delta:.2f} кВт·год за {date_str}."


DISPATCH = {
    "search_knowledge": tool_search_knowledge,
    "get_live_state": tool_get_live_state,
    "get_energy_usage": tool_get_energy_usage,
}


def call_tool(name: str, arguments_json: str) -> str:
    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return "Помилка: невалідні аргументи інструменту."
    handler = DISPATCH.get(name)
    if not handler:
        return f"Невідомий інструмент: {name}"
    try:
        return handler(args)
    except Exception as e:
        return f"Помилка виконання {name}: {e}"
