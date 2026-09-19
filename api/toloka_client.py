"""Toloka.to client — HTML scraping, the site has no API (ADR-0024).

Reads TOLOKA_USER / TOLOKA_PASSWORD from the environment
(api/secrets.enc.env). Only ever talks to https://toloka.to.
"""

import os
import re

import requests
from bs4 import BeautifulSoup

BASE = "https://toloka.to"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — run via sops exec-env secrets.enc.env")
    return value


_USER, _PASSWORD = _env("TOLOKA_USER"), _env("TOLOKA_PASSWORD")
_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0"


def _login() -> None:
    r = _session.post(
        f"{BASE}/login.php",
        data={"username": _USER, "password": _PASSWORD, "autologin": "on", "ssl": "on", "redirect": "", "login": "Вхід"},
        timeout=20,
    )
    r.raise_for_status()
    if "logout" not in r.text.lower():
        raise RuntimeError("Toloka login failed")


def _get(path: str, **kwargs) -> requests.Response:
    """GET, logging in first when the session has no cookie or has expired."""
    if "toloka_sid" not in _session.cookies:
        _login()
    r = _session.get(f"{BASE}/{path}", timeout=30, **kwargs)
    if "logout" not in r.text.lower() and r.headers.get("content-type", "").startswith("text/html"):
        _login()
        r = _session.get(f"{BASE}/{path}", timeout=30, **kwargs)
    r.raise_for_status()
    return r


_SIZE_RE = re.compile(r"^\s*[\d.,]+\s*[KMGT]B\s*$", re.IGNORECASE)


def search(query: str) -> list[dict]:
    """Rows of tracker.php results: forum, title, size, seeders, leechers, ids."""
    soup = BeautifulSoup(_get("tracker.php", params={"nm": query}).text, "html.parser")
    rows = []
    for tr in soup.select("tr.prow1, tr.prow2"):
        title_a = tr.select_one("td.topictitle a")
        dl_a = tr.select_one("a[href^='download.php?id=']")
        seed = tr.select_one("td.seedmed b")
        if not (title_a and dl_a and seed):
            continue  # a layout change should drop rows loudly below, not mis-parse
        forum_a = tr.select_one("a[href^='tracker.php?f=']")
        leech = tr.select_one("td.leechmed b")
        size = next((td.get_text(strip=True) for td in tr.find_all("td") if _SIZE_RE.match(td.get_text())), "?")
        rows.append({
            "title": title_a.get_text(" ", strip=True),
            "topic_id": title_a["href"].lstrip("t"),
            "download_id": dl_a["href"].split("=", 1)[1],
            "forum": forum_a.get_text(strip=True) if forum_a else "?",
            "size": size,
            "seeders": int(seed.get_text(strip=True) or 0),
            "leechers": int(leech.get_text(strip=True) or 0) if leech else 0,
        })
    if not rows and soup.select("td.topictitle"):  # result cells exist but none parsed
        raise RuntimeError("Toloka: сторінку не вдалося розібрати (змінилась верстка?)")
    return rows


def download_torrent(download_id: str) -> bytes:
    """The .torrent bytes; refuses anything that isn't a bencoded dict."""
    if not download_id.isdigit():
        raise ValueError("bad download id")
    data = _get("download.php", params={"id": download_id}).content
    if not (data.startswith(b"d") and b"info" in data[:4096] + data[-4096:]):
        raise RuntimeError("Toloka повернула не .torrent (сесія втрачена чи ліміт?)")
    return data
