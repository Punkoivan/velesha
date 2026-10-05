"""Household kitchen equipment (kitchen.yaml) for recipes written to fit it (ADR-0074).

recipe_add rewrites a recipe's steps so each says which device, attachment and
mode/program to use — chosen ONLY from this catalogue. The file is edited by
hand and re-read when it changes.
"""

import pathlib

import yaml

_PATH = pathlib.Path(__file__).parent / "kitchen.yaml"
_cache: dict = {"mtime": None, "data": {"devices": []}}


def devices() -> list[dict]:
    try:
        mtime = _PATH.stat().st_mtime
        if mtime != _cache["mtime"]:
            _cache["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {"devices": []}
            _cache["mtime"] = mtime
    except (OSError, yaml.YAMLError) as e:
        print(f"kitchen.yaml error: {e}", flush=True)
    return _cache["data"].get("devices") or []


def summary() -> str:
    """One line for the system prompt: what's in the kitchen."""
    return "; ".join(d["name"] for d in devices())


def catalogue() -> str:
    """Full catalogue for the step-adaptation call: devices, attachments, modes, each with its use."""
    out = []
    for d in devices():
        out.append(f"* {d['name']} — {d.get('for', '')}")
        for a in d.get("attachments") or []:
            out.append(f"  - насадка «{a['name']}»: {a.get('for', '')}")
        for m in d.get("modes") or []:
            out.append(f"  - режим «{m['name']}»: {m.get('for', '')}")
    return "\n".join(out)
