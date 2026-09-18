"""Index Tandoor recipes into Qdrant.

Usage (two secret files: Tandoor's own, and the shared Qdrant one —
ADR-0005, ADR-0006):
    sops exec-env ../../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run ingest.py"'
"""

import hashlib
import os

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import tandoor_client
from textify import textify

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")
COLLECTION = "tandoor_recipes"
VECTOR_SIZE = 1024  # bge-m3, see ADR-0002


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


def stable_id(recipe_id: int) -> str:
    return hashlib.sha256(f"tandoor:{recipe_id}".encode()).hexdigest()[:32]


def main() -> None:
    client = qdrant_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}'")

    recipes = tandoor_client.get_recipes()
    print(f"{len(recipes)} recipes in Tandoor")

    points = []
    for recipe in recipes:
        text = textify(recipe)
        vector = embed(text)
        points.append(
            PointStruct(
                id=stable_id(recipe["id"]),
                vector=vector,
                payload={"title": recipe["name"], "recipe_id": recipe["id"], "text": text},
            )
        )
        print(f"embedded: {recipe['name']}")

    if points:
        client.upsert(collection_name=COLLECTION, points=points)
    print(f"\nIndexed {len(points)} Tandoor recipes into '{COLLECTION}'")


if __name__ == "__main__":
    main()
