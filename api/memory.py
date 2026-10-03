"""Long-term memory across conversations and channels (ADR-0069).

One store, every fact owned by a person or by "shared" (the household).
A person sees their own facts plus shared ones; shared devices (the hall
tablet, s21-voice) see and write shared only.

Three ways in: explicit "запам'ятай" (memory_add), automatic extraction of
durable facts after each turn (learn), and personal notes migrated from the
old per-user notes files (ADR-0046). One way out into every answer:
relevant facts for the current message go into the system prompt
(relevant_block / set_block / context_block) — plus memory_search / memory_forget tools.
"""

import contextvars
import datetime as dt
import json
import os
import pathlib
import re
import threading
import uuid
import zoneinfo

import requests

import guardrails
from search_backend import embed

SHARED = "shared"
TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
_PATH = pathlib.Path(__file__).parent / "data" / "memory.jsonl"
_LOCK = threading.Lock()
_DUP_SIM = 0.92        # a new fact this close to a stored one is the same fact
_CONTEXT_SIM = 0.55    # relevant enough to put into the prompt unasked
_SEARCH_SIM = 0.5      # same loose threshold as the old notes search (ADR-0046)
_CONTEXT_MAX = 5

_context: contextvars.ContextVar[str] = contextvars.ContextVar("velesha_memory_context", default="")


def scope(user: str) -> set[str]:
    return {SHARED} if user == SHARED else {user, SHARED}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _load() -> list[dict]:
    if not _PATH.exists():
        return []
    return [json.loads(l) for l in _PATH.read_text(encoding="utf-8").splitlines() if l.strip()]


def _visible(user: str) -> list[dict]:
    allowed = scope(user)
    return [r for r in _load() if r["owner"] in allowed]


def add(text: str, owner: str, source: str) -> tuple[dict, dict | None]:
    """(row, None) when stored; (None-like row, existing) when it's a duplicate."""
    text = " ".join(text.split())
    vec = None
    try:
        vec = [round(x, 5) for x in embed(text)]
    except Exception:
        pass  # embedding server down — saved anyway, found by substring until re-embedded
    with _LOCK:
        if vec:
            same_owner = [r for r in _load() if r["owner"] == owner and r.get("vec")]
            for r in same_owner:
                if _cos(vec, r["vec"]) >= _DUP_SIM:
                    return r, r
        row = {"id": uuid.uuid4().hex[:12], "owner": owner, "text": text, "source": source,
               "at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}
        if vec:
            row["vec"] = vec
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row, None


def search(query: str, user: str, limit: int = 8, threshold: float = _SEARCH_SIM) -> list[dict]:
    rows = _visible(user)
    q = (query or "").strip()
    if not q:
        return rows[-limit:]
    ql = q.lower()
    substr = [r for r in rows if any(w[:5] in r["text"].lower() for w in ql.split() if len(w) >= 3)]
    semantic = []
    try:
        qvec = embed(q)
        scored = sorted(((r, _cos(qvec, r["vec"])) for r in rows if r.get("vec")), key=lambda p: -p[1])
        semantic = [r for r, s in scored[:limit] if s >= threshold]
    except Exception:
        pass
    seen, out = set(), []
    for r in semantic + substr:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out[:limit]


_FORGET_NOISE = re.compile(r"\b(забудь|забути|видали|прибери|з пам'?яті|що|про|це|неправда|будь ласка)\b", re.IGNORECASE)


def forget(query: str, user: str) -> dict | None:
    """Remove the single best-matching visible fact; None when nothing matches clearly."""
    rows = _visible(user)
    q = " ".join(_FORGET_NOISE.sub(" ", query or "").split())
    if not q:
        return None
    words = [w[:5] for w in q.lower().split() if len(w) >= 3]
    hits = [r for r in rows if words and all(w in r["text"].lower() for w in words)]
    if len(hits) == 1:
        victim = hits[0]  # «кінзу» -> the one fact that mentions it
    else:
        try:
            qvec = embed(q)
        except Exception:
            return None
        pool = hits or rows
        scored = sorted(((r, _cos(qvec, r["vec"])) for r in pool if r.get("vec")), key=lambda p: -p[1])
        if not scored or scored[0][1] < 0.5:
            return None
        victim = scored[0][0]
    with _LOCK:
        keep = [r for r in _load() if r["id"] != victim["id"]]
        _PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    return victim


def relevant_block(text: str, user: str) -> str:
    """The facts relevant to this message, as a system-prompt block ("" if none).
    Runs in a worker thread — the caller sets it with set_block in its own context."""
    block = ""
    try:
        if text.strip():
            qvec = embed(text)
            scored = sorted(((r, _cos(qvec, r["vec"])) for r in _visible(user) if r.get("vec")),
                            key=lambda p: -p[1])
            picked = [r for r, s in scored[:_CONTEXT_MAX] if s >= _CONTEXT_SIM]
            if picked:
                block = ("Що ти пам'ятаєш (з попередніх розмов; використовуй, якщо доречно, не переказуй без потреби):\n"
                         + "\n".join(f"- {r['text']}" for r in picked) + "\n")
    except Exception as e:  # memory must never break an answer
        print(f"memory context error: {e}", flush=True)
    return block


def set_block(block: str) -> None:
    _context.set(block)


def context_block() -> str:
    return _context.get()


_LEARN_PROMPT = (
    "Ти ведеш довготривалу пам'ять домашнього асистента. З репліки користувача (і відповіді асистента для "
    "контексту) витягни ЛИШЕ тривалі факти, корисні в майбутніх розмовах: про людей і тварин дому, їхні "
    "вподобання, алергії, звички, техніку і як нею користуються, домовленості, постійні правила дому. "
    "НЕ записуй: запитання, разові дії й команди («увімкни», «додай у список»), поточні стани й кількості "
    "(залишки, температура — вони є в системах), те, що асистент сам сказав, і загальновідомі речі. "
    "Здебільшого фактів немає — тоді порожній список. Кожен факт — одне коротке самодостатнє речення "
    "українською, з іменами, а не займенниками. scope: \"personal\" — про самого користувача (його смаки, "
    "плани), \"shared\" — про дім, тварин, спільну техніку чи інших людей. "
    'Відповідь — JSON {"facts": [{"text": "...", "scope": "personal|shared"}]}.'
)


def _who(user: str) -> str:
    """Tells the extractor who "я" is, so a personal fact names the person, not "Користувач"."""
    names = dict(p.split(":", 1) for p in os.environ.get("VELESHA_NAMES", "").split(",") if ":" in p)
    if user == SHARED:
        return "Говорить хтось із родини зі спільного пристрою (особу невідомо — особисті факти не записуй)."
    return f"Користувача звати {names.get(user, user)} (це відомо — саме ім'я фактом не записуй)."


def _openai_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") if os.environ.get("CHAT_PROVIDER") == "openai" else None


def learn(user_text: str, answer: str, user: str, personal_only: bool = False) -> list[str]:
    """Automatic fact extraction after a turn; returns the texts actually stored.
    personal_only: everything goes to the person's own memory — Telegram (ADR-0071),
    so text typed there can't rewrite what the whole household is told."""
    if len((user_text or "").split()) < 4:
        return []  # "так", "панда", "увімкни світло" — nothing durable in that
    key = _openai_key()
    if not key or guardrails.budget_left() <= 0:
        return []
    body = {
        "model": os.environ.get("CHAT_MODEL", "gpt-4.1-mini"),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LEARN_PROMPT},
            {"role": "user", "content": guardrails.redact(f"{_who(user)}\nКористувач: {user_text}\nАсистент: {answer}")},
        ],
    }
    r = requests.post("https://api.openai.com/v1/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    guardrails.record_usage(data.get("usage", {}).get("total_tokens", 0))
    facts = json.loads(data["choices"][0]["message"]["content"]).get("facts", [])
    stored = []
    for f in facts:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        if personal_only and user != SHARED:
            owner = user
        else:
            owner = SHARED if user == SHARED or f.get("scope") != "personal" else user
        row, dup = add(text, owner, "auto")
        if not dup:
            stored.append(text)
    if stored:
        print(f"memory learned ({user}): {stored}", flush=True)
    return stored


def migrate_notes(notes_dir: pathlib.Path) -> int:
    """One-time: old per-user notes files (ADR-0046) -> memory, owner = file's user
    ("default" — the single-user era — belongs to the admin, VELESHA_DEFAULT_USER)."""
    if not notes_dir.exists():
        return 0
    moved = 0
    default_owner = os.environ.get("VELESHA_DEFAULT_USER", "punka")
    for f in sorted(notes_dir.glob("*.jsonl")):
        owner = default_owner if f.stem == "default" else f.stem
        rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        with _LOCK, _PATH.open("a", encoding="utf-8") as out:
            for n in rows:
                row = {"id": uuid.uuid4().hex[:12], "owner": owner, "text": n["text"], "source": "note", "at": n["at"]}
                if n.get("vec"):
                    row["vec"] = n["vec"]
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                moved += 1
        f.rename(f.with_suffix(".jsonl.migrated"))
    return moved
