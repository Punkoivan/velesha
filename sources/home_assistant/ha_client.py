"""Minimal Home Assistant REST client.

Reads HA_URL / HA_TOKEN from the environment (populated by
`sops exec-env secrets.enc.env '...'`), not from a file — see ADR-0005.
"""

import os

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Run this via:\n"
            f"  sops exec-env secrets.enc.env 'uv run <script>.py'"
        )
    return value


HA_URL = _env("HA_URL").rstrip("/")
HA_TOKEN = _env("HA_TOKEN")

_session = requests.Session()
_session.headers.update(
    {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
)
_session.verify = False


def get_states() -> list[dict]:
    r = _session.get(f"{HA_URL}/api/states", timeout=30)
    r.raise_for_status()
    return r.json()


def get_history(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    r = _session.get(
        f"{HA_URL}/api/history/period/{start_iso}",
        params={"filter_entity_id": entity_id, "end_time": end_iso},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []
