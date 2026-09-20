"""Who is calling (ADR-0035).

HA has no user identity for OpenAI-compatible agents, so each person gets
their own conversation-agent entry in HA whose API key selects the user here.
VELESHA_USERS="key1:ivan,key2:olha"; VELESHA_ADMINS="ivan" (default: first).
Unset VELESHA_USERS = single-user mode: no auth, the caller is an admin.
"""
import contextvars
import os
import re

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_current: contextvars.ContextVar[str] = contextvars.ContextVar("velesha_user", default="")


def _users() -> dict[str, str]:
    users = {}
    for pair in filter(None, (p.strip() for p in os.environ.get("VELESHA_USERS", "").split(","))):
        key, _, name = pair.partition(":")
        if key and _NAME_RE.match(name):
            users[key] = name
    return users


def _admins() -> set[str]:
    named = {a.strip() for a in os.environ.get("VELESHA_ADMINS", "").split(",") if a.strip()}
    if named:
        return named
    first = next(iter(_users().values()), None)
    return {first} if first else set()


def resolve(authorization: str | None) -> str | None:
    """User name for a request, or None when the key is unknown."""
    users = _users()
    if not users:
        return "default"
    token = (authorization or "").removeprefix("Bearer ").strip()
    return users.get(token)


def set_current(name: str) -> None:
    _current.set(name)


def current() -> str:
    return _current.get() or "default"


def is_admin(name: str | None = None) -> bool:
    name = name or current()
    return name == "default" and not _users() or name in _admins()
