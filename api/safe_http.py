"""Fetching a page from a URL a user handed us, without becoming a way into the LAN (ADR-0080).

A link like http://192.168.88.80:8123/... or http://localhost:8083 would otherwise
make this server call its own neighbours (SSRF). Every hop — the first URL
and each redirect — must be http(s) on the default port and resolve only to
public addresses; the body is capped and must be HTML. Only what the caller
extracts (a title) leaves this module — never the page text.

Residual risk: DNS rebinding (a name that resolves to a public IP for the
check and a private one for the request a moment later). It needs an
attacker-controlled DNS name sent by an allowlisted user; accepted for a
home setup.
"""

import ipaddress
import socket
import urllib.parse

import requests

_MAX_BYTES = 512 * 1024
_MAX_HOPS = 3
_UA = {"User-Agent": "Velesha/1.0 (home assistant; film lookup)"}


class Blocked(ValueError):
    """The URL points somewhere we must not go."""


def check(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise Blocked(f"схема «{p.scheme}» не дозволена")
    if not p.hostname:
        raise Blocked("немає адреси сайту")
    if p.port not in (None, 80, 443):
        raise Blocked(f"порт {p.port} не дозволений")
    if p.username or p.password:
        raise Blocked("посилання з логіном/паролем не відкриваю")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))}
    except socket.gaierror:
        raise Blocked(f"сайт «{p.hostname}» не знайдено")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise Blocked(f"«{p.hostname}» веде в локальну мережу ({ip}) — туди не ходжу")


def fetch_html(url: str) -> str:
    """Body of an HTML page at a public URL, at most _MAX_BYTES; Blocked otherwise."""
    for _ in range(_MAX_HOPS + 1):
        check(url)
        with requests.get(url, headers=_UA, timeout=(5, 10), allow_redirects=False, stream=True) as r:
            if r.is_redirect or r.is_permanent_redirect:
                url = urllib.parse.urljoin(url, r.headers.get("location", ""))
                continue  # the new address is checked like the first one
            if not r.ok:
                raise Blocked(f"сайт відповів HTTP {r.status_code}")
            if "html" not in r.headers.get("content-type", "").lower():
                raise Blocked("це не веб-сторінка")
            body = b""
            for chunk in r.iter_content(16384):
                body += chunk
                if len(body) >= _MAX_BYTES:
                    break
            return body[:_MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
    raise Blocked("забагато переадресацій")


def is_torrent_link(url: str) -> bool:
    """What may go to qBittorrent: a magnet link, or an https link on Toloka itself."""
    if url.lower().startswith("magnet:?"):
        return True
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "toloka.to" or host.endswith(".toloka.to")) and p.port in (None, 443)
