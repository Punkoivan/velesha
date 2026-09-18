"""Minimal Jellyfin REST client.

Reads JELLYFIN_URL / JELLYFIN_API_KEY / JELLYFIN_USER_ID from the
environment (populated by `sops exec-env secrets.enc.env '...'`), not from
a file — see ADR-0005.

Watch stats (UserData: Played, PlayCount, LastPlayedDate, ...) are
per-user in Jellyfin, so a user id is required — see ADR-0007 for why
we scope to a single user rather than aggregating across the household.
"""

import os

import requests


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Run this via:\n"
            f"  sops exec-env secrets.enc.env 'uv run <script>.py'"
        )
    return value


JELLYFIN_URL = _env("JELLYFIN_URL").rstrip("/")
JELLYFIN_API_KEY = _env("JELLYFIN_API_KEY")
JELLYFIN_USER_ID = _env("JELLYFIN_USER_ID")

_session = requests.Session()
_session.headers.update({"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'})


def get_items(item_type: str) -> list[dict]:
    """Fetch every item of one type (Movie, Series, ...) for JELLYFIN_USER_ID."""
    r = _session.get(
        f"{JELLYFIN_URL}/Users/{JELLYFIN_USER_ID}/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": item_type,
            "Fields": "Overview,Genres,ProductionYear,UserData",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["Items"]
