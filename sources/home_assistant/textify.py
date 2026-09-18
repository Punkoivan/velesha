"""Turn a HA state-change event into a short Ukrainian sentence.

See ADR-0004: this is the actual complexity of HA ingestion — the
embedding step itself is generic.
"""

import datetime


def _fmt_time(iso: str) -> str:
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.strftime("%H:%M, %d.%m.%Y")


# domain-specific phrasing; falls back to a generic template if no match
_ON_OFF = {"on": "увімкнено", "off": "вимкнено"}
_LOCK = {"locked": "замкнено", "unlocked": "розблоковано"}
_OPEN_CLOSED = {"open": "відчинено", "closed": "зачинено"}
_HOME = {"home": "вдома", "not_home": "відсутній(-я)"}
_BINARY_MOTION = {"on": "виявлено рух", "off": "рух не виявляється"}
_BINARY_GENERIC = {"on": "спрацював", "off": "неактивний"}


def textify(event: dict) -> str | None:
    """Return a sentence for one state-change event, or None to skip it."""
    entity_id = event["entity_id"]
    domain = entity_id.split(".", 1)[0]
    state = event.get("state")
    attrs = event.get("attributes", {})
    name = attrs.get("friendly_name", entity_id)
    when = _fmt_time(event["last_changed"])

    if state in ("unknown", "unavailable"):
        return None

    if domain in ("light", "switch", "fan"):
        verb = _ON_OFF.get(state)
        if verb is None:
            return None
        return f"{name}: {verb} о {when}"

    if domain == "lock":
        verb = _LOCK.get(state)
        return f"{name}: {verb} о {when}" if verb else None

    if domain == "cover":
        verb = _OPEN_CLOSED.get(state)
        return f"{name}: {verb} о {when}" if verb else None

    if domain == "person" or domain == "device_tracker":
        verb = _HOME.get(state)
        return f"{name}: {verb} о {when}" if verb else None

    if domain == "climate":
        target = attrs.get("temperature")
        extra = f", цільова температура {target}°C" if target is not None else ""
        return f"{name}: режим '{state}'{extra}, о {when}"

    if domain == "binary_sensor":
        device_class = attrs.get("device_class", "")
        if device_class == "motion":
            verb = _BINARY_MOTION.get(state)
        elif device_class in ("door", "window", "opening", "garage_door"):
            verb = _OPEN_CLOSED.get({"on": "open", "off": "closed"}.get(state))
        else:
            verb = _BINARY_GENERIC.get(state)
        return f"{name} ({device_class or 'сенсор'}): {verb} о {when}" if verb else None

    if domain == "alarm_control_panel":
        return f"{name}: стан '{state}' о {when}"

    if domain == "media_player":
        if state in ("playing", "paused", "off", "idle"):
            return f"{name}: {state} о {when}"
        return None

    return None


# Domains worth ingesting at all — everything else (raw sensors, updates,
# automations, etc.) is skipped to keep the collection meaningful instead
# of drowning in noisy numeric telemetry. See ADR-0004.
INTERESTING_DOMAINS = {
    "light",
    "switch",
    "fan",
    "lock",
    "cover",
    "person",
    "device_tracker",
    "climate",
    "binary_sensor",
    "alarm_control_panel",
    "media_player",
}
