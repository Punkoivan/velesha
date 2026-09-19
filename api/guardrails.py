"""Data-leak and cost guardrails for hosted-model calls (ADR-0025).

Everything here runs in our process, before a request leaves the machine
— an agentgateway in front is a second layer, not a replacement.
Local-model calls are never touched (nothing leaves the machine).
"""

import datetime
import json
import os
import pathlib
import re
from urllib.parse import urlparse

# Tool results of these tools are never sent to a hosted model; the rest of
# the request is finished by the local model instead. Default = the user's
# choice (ADR-0025): home state and history/consumption stay local. Env
# override, comma-separated (empty string = block nothing).
BLOCKED_TOOLS = {
    t.strip()
    for t in os.environ.get("HOSTED_BLOCKED_TOOLS", "get_live_state,get_sensor_history").split(",")
    if t.strip()
}
# search_knowledge is allowed in general (recipes, Jellyfin), but rows from
# the ha_history collection are the same class as get_sensor_history.
_HISTORY_MARKER = "[ha_history]"
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "500000"))
_USAGE_FILE = pathlib.Path(__file__).parent / "data" / "usage.json"

_PATTERNS = [
    (re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), "[токен]"),  # JWT (HA tokens)
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[ключ]"),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), ""),  # user:pass@ in URLs
    (re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"), "[внутрішня адреса]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[id]"),  # API keys, user ids, torrent hashes
]


def _secret_values() -> list[str]:
    """Exact values of every secret-looking env var, longest first."""
    vals = [
        v for k, v in os.environ.items()
        if re.search(r"(TOKEN|KEY|PASSWORD|SECRET)", k) and len(v) >= 6
    ]
    return sorted(set(vals), key=len, reverse=True)


def _hosts() -> list[str]:
    """Hostnames of our own services (HA, Jellyfin, ...), from *_URL vars."""
    hosts = set()
    for k, v in os.environ.items():
        if k.endswith("_URL") and "://" in v:
            host = urlparse(v).hostname
            if host and not host.startswith(("localhost", "127.")):
                hosts.add(host)
    return sorted(hosts, key=len, reverse=True)


def redact(text: str) -> str:
    for secret in _secret_values():
        text = text.replace(secret, "[секрет]")
    for host in _hosts():
        text = text.replace(host, "[хост]")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_messages(messages: list[dict]) -> list[dict]:
    """A redacted copy, safe to send to a hosted model."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), str):
            m["content"] = redact(m["content"])
        if m.get("tool_calls"):
            m["tool_calls"] = [
                {**tc, "function": {**tc["function"], "arguments": redact(tc["function"]["arguments"])}}
                for tc in m["tool_calls"]
            ]
        out.append(m)
    return out


def has_blocked_result(messages: list[dict], tool_names: dict[str, str]) -> bool:
    """True if any tool result in the conversation came from a blocked tool."""
    for m in messages:
        if m.get("role") != "tool":
            continue
        name = tool_names.get(m.get("tool_call_id"))
        if name in BLOCKED_TOOLS:
            return True
        if name == "search_knowledge" and "ha_history" not in os.environ.get("HOSTED_ALLOW_HISTORY", ""):
            if _HISTORY_MARKER in (m.get("content") or "") and "get_sensor_history" in BLOCKED_TOOLS:
                return True
    return False


def _load() -> dict:
    try:
        return json.loads(_USAGE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _today() -> str:
    return datetime.date.today().isoformat()


def budget_left() -> int:
    return DAILY_TOKEN_BUDGET - _load().get(_today(), 0)


def record_usage(tokens: int) -> None:
    data = {_today(): _load().get(_today(), 0) + tokens}  # older days dropped
    _USAGE_FILE.parent.mkdir(exist_ok=True)
    _USAGE_FILE.write_text(json.dumps(data))
