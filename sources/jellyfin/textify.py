"""Turn Jellyfin library items into short Ukrainian sentences for embedding.

See ADR-0007: movie/series granularity (not per-episode), and why titles
get a light cleanup pass before going into the sentence.
"""

import re

# Release-group tags always show up in square brackets in this library
# (encoding/codec/subtitle info, group name) — safe to strip unconditionally.
_BRACKET_TAG = re.compile(r"\[.*?\]")
# Leading playlist/track numbering, e.g. "01. The Mummy" -> "The Mummy".
_LEADING_INDEX = re.compile(r"^\d+\.\s*")


def clean_title(name: str) -> str:
    title = _BRACKET_TAG.sub("", name)
    title = _LEADING_INDEX.sub("", title)
    return " ".join(title.split())


def _last_played(user_data: dict) -> str | None:
    date = user_data.get("LastPlayedDate")
    return date.split("T")[0] if date else None


def textify_movie(item: dict) -> str:
    title = clean_title(item["Name"])
    year = item.get("ProductionYear")
    year_part = f" ({year})" if year else ""
    ud = item.get("UserData", {})

    if not ud.get("Played"):
        return f"Фільм «{title}»{year_part} ще не переглянуто."

    count = ud.get("PlayCount", 0)
    last = _last_played(ud)
    last_part = f", останній раз {last}" if last else ""
    return f"Фільм «{title}»{year_part} переглянуто {count} раз(и){last_part}."


def textify_series(item: dict) -> str:
    title = clean_title(item["Name"])
    year = item.get("ProductionYear")
    year_part = f" ({year})" if year else ""
    ud = item.get("UserData", {})
    pct = ud.get("PlayedPercentage")

    if not pct:
        return f"Серіал «{title}»{year_part} ще не переглянуто."
    if pct >= 100:
        return f"Серіал «{title}»{year_part} переглянуто повністю."
    return f"Серіал «{title}»{year_part} переглянуто на {pct:.0f}%."


def textify(item: dict) -> str | None:
    item_type = item.get("Type")
    if item_type == "Movie":
        return textify_movie(item)
    if item_type == "Series":
        return textify_series(item)
    return None
