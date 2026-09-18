"""Minimal Tandoor Recipes REST client.

Reads TANDOOR_URL / TANDOOR_TOKEN from the environment (populated by
`sops exec-env secrets.enc.env '...'`), not from a file — see ADR-0005.
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


TANDOOR_URL = _env("TANDOOR_URL").rstrip("/")
TANDOOR_TOKEN = _env("TANDOOR_TOKEN")

_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {TANDOOR_TOKEN}"})


def get_recipes() -> list[dict]:
    """Fetch every recipe's full detail (steps, ingredients), following pagination."""
    recipes = []
    url = f"{TANDOOR_URL}/api/recipe/"
    while url:
        r = _session.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        for summary in data["results"]:
            detail = _session.get(f"{TANDOOR_URL}/api/recipe/{summary['id']}/", timeout=30)
            detail.raise_for_status()
            recipes.append(detail.json())
        url = data.get("next")
    return recipes
