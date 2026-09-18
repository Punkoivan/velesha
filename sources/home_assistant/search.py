"""Semantic search over indexed Home Assistant history.

Usage (needs the shared Qdrant secret, not the HA one — search doesn't
call the HA API):
    sops exec-env ../../secrets.enc.env 'uv run search.py "коли я востаннє вмикав опалення"'
"""

import os
import sys

import requests
from qdrant_client import QdrantClient

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")
COLLECTION = "ha_history"


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


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run search.py <query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    client = qdrant_client()
    vector = embed(query)

    results = client.query_points(
        collection_name=COLLECTION, query=vector, limit=5, with_payload=True
    ).points

    for r in results:
        print(f"{r.score:.3f}  {r.payload['text']}")


if __name__ == "__main__":
    main()
