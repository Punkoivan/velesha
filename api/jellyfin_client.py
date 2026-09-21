"""Minimal Jellyfin client for the play-on-device tool (ADR-0021).

Reads JELLYFIN_URL / JELLYFIN_API_KEY / JELLYFIN_USER_ID from the
environment (api/secrets.enc.env), same server and user as
sources/jellyfin/.
"""

import os

import requests


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — run via sops exec-env secrets.enc.env")
    return value


URL = _env("JELLYFIN_URL").rstrip("/")
USER_ID = _env("JELLYFIN_USER_ID")

_session = requests.Session()
_session.headers.update({"Authorization": f'MediaBrowser Token="{_env("JELLYFIN_API_KEY")}"'})


def search(term: str) -> list[dict]:
    r = _session.get(
        f"{URL}/Users/{USER_ID}/Items",
        params={"Recursive": "true", "SearchTerm": term, "IncludeItemTypes": "Series,Movie", "Limit": 10},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["Items"]


def next_episode(series_id: str) -> dict | None:
    """Next unwatched episode, else the very first one."""
    r = _session.get(f"{URL}/Shows/NextUp", params={"SeriesId": series_id, "UserId": USER_ID, "Limit": 1}, timeout=15)
    r.raise_for_status()
    items = r.json()["Items"]
    if items:
        return items[0]
    r = _session.get(f"{URL}/Shows/{series_id}/Episodes", params={"UserId": USER_ID, "Limit": 1}, timeout=15)
    r.raise_for_status()
    items = r.json()["Items"]
    return items[0] if items else None


def sessions() -> list[dict]:
    r = _session.get(f"{URL}/Sessions", timeout=15)
    r.raise_for_status()
    return r.json()


def play(session_id: str, item_id: str) -> None:
    r = _session.post(
        f"{URL}/Sessions/{session_id}/Playing",
        params={"playCommand": "PlayNow", "itemIds": item_id},
        timeout=15,
    )
    r.raise_for_status()


def playstate(session_id: str, command: str) -> None:
    """Pause / Unpause / Stop on an existing playback session."""
    r = _session.post(f"{URL}/Sessions/{session_id}/Playing/{command}", timeout=15)
    r.raise_for_status()


def adjacent_episode(series_id: str, season: int, index: int, step: int) -> dict | None:
    """The episode `step` positions after (+1) or before (-1) season/index."""
    r = _session.get(f"{URL}/Shows/{series_id}/Episodes", params={"UserId": USER_ID}, timeout=15)
    r.raise_for_status()
    eps = sorted(
        (e for e in r.json()["Items"] if e.get("ParentIndexNumber") is not None and e.get("IndexNumber") is not None),
        key=lambda e: (e["ParentIndexNumber"], e["IndexNumber"]),
    )
    for i, e in enumerate(eps):
        if (e["ParentIndexNumber"], e["IndexNumber"]) == (season, index):
            j = i + step
            return eps[j] if 0 <= j < len(eps) else None
    return None


def get_item(item_id: str) -> dict:
    r = _session.get(f"{URL}/Users/{USER_ID}/Items/{item_id}", timeout=15)
    r.raise_for_status()
    return r.json()


def set_played(item_id: str, played: bool) -> None:
    r = _session.request("POST" if played else "DELETE", f"{URL}/Users/{USER_ID}/PlayedItems/{item_id}", timeout=15)
    r.raise_for_status()
