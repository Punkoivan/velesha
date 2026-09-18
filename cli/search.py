"""Unified semantic search across every indexed Velesha source.

Queries every known Qdrant collection with the same embedding and merges
results by score — one search surface instead of a per-source script
(`sources/*/search.py`), per PLAN.md Phase 2.

Usage (needs the shared Qdrant secret — ADR-0006):
    sops exec-env ../secrets.enc.env 'uv run search.py "коли вмикали телевізор"'
    sops exec-env ../secrets.enc.env 'uv run search.py --source obsidian_recipes "курка"'
"""

import argparse
import os

import requests
from qdrant_client import QdrantClient

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")

# One entry per source collection — kept in sync by hand as sources are
# added (sources/obsidian, sources/home_assistant, sources/jellyfin,
# sources/tandoor).
COLLECTIONS = ["obsidian_recipes", "ha_history", "jellyfin_library", "tandoor_recipes"]


def qdrant_client() -> QdrantClient:
    """See ADR-0006: shared ha-addon-qdrant instance, not abox."""
    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    kwargs = {}
    if api_key := os.environ.get("QDRANT_API_KEY"):
        kwargs["api_key"] = api_key
    return QdrantClient(url=url, **kwargs)


def embed(text: str) -> list[float]:
    resp = requests.post(EMBED_URL, json={"content": text}, timeout=60)
    resp.raise_for_status()
    embedding = resp.json()[0]["embedding"]
    if isinstance(embedding[0], list):
        embedding = embedding[0]
    return embedding


def search_all(client: QdrantClient, vector: list[float], collections: list[str], per_collection: int) -> list[dict]:
    hits = []
    for collection in collections:
        if not client.collection_exists(collection):
            continue
        results = client.query_points(
            collection_name=collection,
            query=vector,
            limit=per_collection,
            with_payload=True,
        ).points
        for r in results:
            hits.append({"collection": collection, "score": r.score, "payload": r.payload})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument(
        "--source",
        choices=COLLECTIONS,
        help="restrict to one collection instead of searching all of them",
    )
    parser.add_argument("--limit", type=int, default=5, help="results to show")
    parser.add_argument("--full", action="store_true", help="print full text, not a snippet")
    args = parser.parse_args()

    query = " ".join(args.query)
    collections = [args.source] if args.source else COLLECTIONS

    client = qdrant_client()
    vector = embed(query)
    hits = search_all(client, vector, collections, per_collection=args.limit)

    for h in hits[: args.limit]:
        payload = h["payload"]
        text = payload.get("text", "")
        if not args.full:
            title = payload.get("title") or payload.get("name")
            snippet = " ".join(text.split())[:150]
            text = f"{title} — {snippet}" if title else snippet
        print(f"{h['score']:.3f}  [{h['collection']}]  {text}")


if __name__ == "__main__":
    main()
