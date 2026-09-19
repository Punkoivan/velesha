"""Minimal qBittorrent Web API client (v5) — ADR-0023.

Reads QBIT_URL / QBIT_USER / QBIT_PASSWORD from the environment
(api/secrets.enc.env). Logs in lazily and re-logs in on a 403.
"""

import os

import requests


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — run via sops exec-env secrets.enc.env")
    return value


URL = _env("QBIT_URL").rstrip("/")
_USER, _PASSWORD = _env("QBIT_USER"), _env("QBIT_PASSWORD")
_session = requests.Session()


def _login() -> None:
    r = _session.post(f"{URL}/api/v2/auth/login", data={"username": _USER, "password": _PASSWORD}, timeout=15)
    r.raise_for_status()
    if r.text.strip() == "Fails.":
        raise RuntimeError("qBittorrent login failed")


def _request(method: str, path: str, **kwargs) -> requests.Response:
    for attempt in range(2):
        r = _session.request(method, f"{URL}/api/v2{path}", timeout=20, **kwargs)
        if r.status_code == 403 and attempt == 0:
            _login()
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def server_state() -> dict:
    return _request("GET", "/sync/maindata").json()["server_state"]


def torrents() -> list[dict]:
    return _request("GET", "/torrents/info").json()


def categories() -> dict:
    return _request("GET", "/torrents/categories").json()


def add(url: str, category: str) -> bool:
    """autoTMM=true so the file lands in the category's own save path."""
    r = _request("POST", "/torrents/add", data={"urls": url, "category": category, "autoTMM": "true"})
    return r.text.strip() != "Fails."


def add_file(torrent: bytes, category: str, start: bool = True) -> bool:
    """Upload a .torrent; autoTMM=true so it lands in the category's folder."""
    r = _request(
        "POST",
        "/torrents/add",
        data={"category": category, "autoTMM": "true", "stopped": "false" if start else "true"},
        files={"torrents": ("upload.torrent", torrent, "application/x-bittorrent")},
    )
    return r.text.strip() != "Fails."
