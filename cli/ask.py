"""Ask a question in natural language: retrieve relevant chunks from every
source, then have a local chat model answer using them — Phase 2's Q&A
item. See ADR-0010 for the chat model choice.

Usage (needs the shared Qdrant secret — ADR-0006):
    sops exec-env ../secrets.enc.env 'uv run ask.py "коли я востаннє дивився мумію?"'
"""

import argparse
import os

import requests

from search import COLLECTIONS, embed, qdrant_client, search_all

CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8084/v1/chat/completions")

SYSTEM_PROMPT = (
    "Ти — Велеша, персональний асистент. Відповідай українською, коротко "
    "і по суті, спираючись ТІЛЬКИ на наданий контекст. Якщо в контексті "
    "немає відповіді — так і скажи, не вигадуй."
)


MAX_CHUNK_CHARS = 600  # keeps a few chunks + question well under the 8192-token chat context
CANDIDATE_POOL = 20  # see ADR-0017 — wider semantic net before picking by recency


def pick_chunks(hits: list[dict], collections: list[str], per_collection: int) -> list[dict]:
    # ha_history entries for one entity are near-duplicate text
    # ("телевізор: [у]вимкнено о HH:MM, DD.MM.YYYY"), so the true most
    # recent event isn't reliably inside a narrow top-K by cosine score
    # alone (confirmed in testing — it was missing outright). search_all
    # was called with a wide CANDIDATE_POOL; here we pick, per collection,
    # the per_collection most recent by timestamp (ha_history's
    # last_changed) where present, else the top by score (stable sort,
    # missing timestamp treated as empty — same order Qdrant returned).
    # See ADR-0017.
    picked = []
    for collection in collections:
        group = [h for h in hits if h["collection"] == collection]
        group.sort(key=lambda h: h["payload"].get("last_changed") or "", reverse=True)
        picked.extend(group[:per_collection])
    return picked


def build_context(hits: list[dict]) -> str:
    lines = []
    for h in hits:
        text = " ".join(h["payload"].get("text", "").split())[:MAX_CHUNK_CHARS]
        lines.append(f"[{h['collection']}] {text}")
    return "\n".join(lines)


def ask_llm(question: str, context: str) -> str:
    resp = requests.post(
        CHAT_URL,
        json={
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Контекст:\n{context}\n\nПитання: {question}"},
            ],
            "max_tokens": 400,
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="+")
    parser.add_argument(
        "--source",
        choices=COLLECTIONS,
        help="restrict retrieval to one collection instead of searching all of them",
    )
    parser.add_argument(
        "--chunks", type=int, default=4, help="chunks to retrieve per collection (not a global cap)"
    )
    args = parser.parse_args()

    question = " ".join(args.question)
    collections = [args.source] if args.source else COLLECTIONS

    client = qdrant_client()
    vector = embed(question)
    # Per-collection limit, not a global top-N after merging — otherwise a
    # broad recipe question loses recipe results to unrelated but
    # higher-scoring hits from other collections (e.g. HA history events
    # mentioning "духовка" outscoring actual baking recipes). See ADR-0016.
    pool = search_all(client, vector, collections, per_collection=CANDIDATE_POOL)
    hits = pick_chunks(pool, collections, per_collection=args.chunks)

    if not hits:
        print("Нічого релевантного не знайдено в жодній колекції.")
        return

    context = build_context(hits)
    answer = ask_llm(question, context)
    print(answer)


if __name__ == "__main__":
    main()
