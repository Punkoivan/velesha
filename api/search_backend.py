"""Shared Qdrant/embedding backend for api/main.py and api/tools.py.

Kept as a plain module (not duplicated) since both are the same uv
project — unlike the deliberate duplication across sources/*/cli/,
which are separate projects (ADR-0011).
"""

import os

import requests
from qdrant_client import QdrantClient

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")

# obsidian_recipes intentionally excluded: recipes migrated to Tandoor,
# see ADR-0015 — the collection still exists in Qdrant but is stale.
COLLECTIONS = ["ha_history", "jellyfin_library", "tandoor_recipes"]
CHUNKS_PER_COLLECTION = 4  # see ADR-0016 — per collection, not a global cap
CANDIDATE_POOL = 20  # see ADR-0017 — wider semantic net before picking by recency
MAX_CHUNK_CHARS = 600  # see ADR-0010 — keeps the chat model's context from overflowing


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
