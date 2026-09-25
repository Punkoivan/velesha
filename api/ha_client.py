"""Minimal Home Assistant REST client for live tool calls (not ingestion).

Reads HA_URL / HA_TOKEN from the environment — own copy in
api/secrets.enc.env, same instance as sources/home_assistant/ (ADR-0018).
"""

import json
import os

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


def get_states() -> list[dict]:
    r = _session.get(f"{HA_URL}/api/states", timeout=15)
    r.raise_for_status()
    return r.json()


def get_history(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    r = _session.get(
        f"{HA_URL}/api/history/period/{start_iso}",
        params={"filter_entity_id": entity_id, "end_time": end_iso},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []


def call_service(domain: str, service: str, entity_id: str, **data) -> None:
    r = _session.post(f"{HA_URL}/api/services/{domain}/{service}", json={"entity_id": entity_id, **data}, timeout=15)
    r.raise_for_status()


def get_state(entity_id: str) -> dict:
    r = _session.get(f"{HA_URL}/api/states/{entity_id}", timeout=15)
    r.raise_for_status()
    return r.json()


def call_service_data(domain: str, service: str, target: str, **data) -> None:
    r = _session.post(f"{HA_URL}/api/services/{domain}/{service}",
                      json={"entity_id": target, **data}, timeout=15)
    r.raise_for_status()


def notify(service: str, title: str, message: str) -> None:
    """service is the bare name (e.g. "mobile_app_punkas26"), not "notify.<name>"."""
    r = _session.post(f"{HA_URL}/api/services/notify/{service}", json={"title": title, "message": message}, timeout=15)
    r.raise_for_status()


def calendar_events(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    r = _session.get(f"{HA_URL}/api/calendars/{entity_id}", params={"start": start_iso, "end": end_iso}, timeout=15)
    r.raise_for_status()
    return r.json()


def delete_calendar_event(entity_id: str, uid: str) -> bool:
    """No REST service for this (local_calendar exposes only create_event/get_events) —
    the frontend's own calendar card deletes via this websocket command instead."""
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
            await ws.send(json.dumps({"id": 1, "type": "calendar/event/delete", "entity_id": entity_id, "uid": uid}))
            return json.loads(await ws.recv())

    return bool(asyncio.run(_call()).get("success"))
