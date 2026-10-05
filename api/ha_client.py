"""Minimal Home Assistant REST client for live tool calls (not ingestion).

Reads HA_URL / HA_TOKEN from the environment — own copy in
api/secrets.enc.env, same instance as sources/home_assistant/ (ADR-0018).
"""

import json
import os
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — run via sops exec-env secrets.enc.env")
    return value


HA_URL = _env("HA_URL").rstrip("/")
HA_TOKEN = _env("HA_TOKEN")

_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"})
_session.verify = False


def _request(method: str, url: str, *, retries: int = 1, backoff: float = 3, **kwargs) -> requests.Response:
    """Every plain HTTP call in this module goes through here — one retry
    after a short pause by default. HA itself answers fast; failures this
    retry catches are downstream physical/cloud-backed devices (a vacuum
    still processing the previous command, etc.) that occasionally 500 or
    time out and then work fine moments later (observed: vacuum_control)."""
    attempt = 0
    while True:
        try:
            r = _session.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            if attempt >= retries:
                raise
            attempt += 1
            time.sleep(backoff)


def get_states() -> list[dict]:
    return _request("GET", f"{HA_URL}/api/states", timeout=15).json()


def get_history(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    data = _request(
        "GET", f"{HA_URL}/api/history/period/{start_iso}",
        params={"filter_entity_id": entity_id, "end_time": end_iso}, timeout=30,
    ).json()
    return data[0] if data else []


def call_service(domain: str, service: str, entity_id: str, **data) -> None:
    # 30s, not 15 — a real device (e.g. a vacuum waking from its dock) can take
    # longer than 15s to acknowledge a service call (observed: vacuum_control
    # timing out on a real Roborock start).
    _request("POST", f"{HA_URL}/api/services/{domain}/{service}", json={"entity_id": entity_id, **data}, timeout=30)


def get_state(entity_id: str) -> dict:
    return _request("GET", f"{HA_URL}/api/states/{entity_id}", timeout=15).json()


def call_service_data(domain: str, service: str, target: str, **data) -> None:
    _request("POST", f"{HA_URL}/api/services/{domain}/{service}", json={"entity_id": target, **data}, timeout=30)


def notify(service: str, title: str, message: str) -> None:
    """service is the bare name (e.g. "mobile_app_punkas26"), not "notify.<name>"."""
    _request("POST", f"{HA_URL}/api/services/notify/{service}", json={"title": title, "message": message}, timeout=15)


def calendar_events(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    return _request("GET", f"{HA_URL}/api/calendars/{entity_id}",
                     params={"start": start_iso, "end": end_iso}, timeout=15).json()


def _ws_command(msg: dict) -> dict:
    """Run one HA websocket command synchronously — for the handful of things
    (calendar event delete, area registry) with no plain REST equivalent."""
    import asyncio
    import ssl

    import websockets

    async def _call() -> dict:
        ws_url = HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with websockets.connect(ws_url, ssl=ctx if ws_url.startswith("wss") else None, max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            await ws.recv()
            await ws.send(json.dumps({"id": 1, **msg}))
            return json.loads(await ws.recv())

    return asyncio.run(_call())


def delete_calendar_event(entity_id: str, uid: str) -> bool:
    """No REST service for this (local_calendar exposes only create_event/get_events) —
    the frontend's own calendar card deletes via this websocket command instead."""
    return bool(_ws_command({"type": "calendar/event/delete", "entity_id": entity_id, "uid": uid}).get("success"))


def entity_aliases(entity_id: str) -> list[str]:
    """Names the household gave an entity in HA (Settings → entity → aliases), e.g.
    "Бичок" for the vacuum. Websocket only, like the area registry."""
    r = _ws_command({"type": "config/entity_registry/get", "entity_id": entity_id}).get("result") or {}
    return [a for a in (r.get("aliases") or []) if a]


def area_registry() -> list[dict]:
    """Not exposed over plain REST (/api/config/area_registry -> 404) — same
    websocket the frontend's own area settings page uses."""
    return _ws_command({"type": "config/area_registry/list"}).get("result") or []
