"""Index Obsidian recipes into Qdrant using a local llama.cpp embedding server.

Usage (needs the shared Qdrant secret — ADR-0006):
    sops exec-env ../../secrets.enc.env 'uv run index.py'
"""

import hashlib
import os
import pathlib
import sys

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

RECIPES_DIR = pathlib.Path("/home/punka/obsidian/Домашнє/рецепти")
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")
COLLECTION = "obsidian_recipes"
VECTOR_SIZE = 1024  # bge-m3 output dimension


def qdrant_client() -> QdrantClient:
    """Connects to the shared ha-addon-qdrant instance — see ADR-0006.

    Reads QDRANT_URL / QDRANT_API_KEY from the environment (populate via
    `sops exec-env ../../secrets.enc.env '...'`). TLS is a real Let's
    Encrypt cert (*.punka.space) — no custom CA needed.
    """
    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    kwargs = {}
    if api_key := os.environ.get("QDRANT_API_KEY"):
        kwargs["api_key"] = api_key
    return QdrantClient(url=url, **kwargs)


def embed(text: str) -> list[float]:
    resp = requests.post(EMBED_URL, json={"content": text}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    # llama.cpp /embedding returns a list with one entry per input;
    # each entry's "embedding" is a list of token-level vectors for
    # pooling_type != none it's already pooled to a single vector.
    embedding = data[0]["embedding"]
    if isinstance(embedding[0], list):
        embedding = embedding[0]
    return embedding


def stable_id(path: pathlib.Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:32]


def main() -> None:
    client = qdrant_client()

    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}'")

    files = sorted(RECIPES_DIR.glob("*.md"))
    if not files:
        print(f"No recipes found in {RECIPES_DIR}", file=sys.stderr)
        sys.exit(1)

    points = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        title = path.stem
        vector = embed(f"{title}\n\n{text}")
        points.append(
            PointStruct(
                id=stable_id(path),
                vector=vector,
                payload={"title": title, "path": str(path), "text": text},
            )
        )
        print(f"embedded: {title}")

    client.upsert(collection_name=COLLECTION, points=points)
    print(f"\nIndexed {len(points)} recipes into '{COLLECTION}'")


if __name__ == "__main__":
    main()
