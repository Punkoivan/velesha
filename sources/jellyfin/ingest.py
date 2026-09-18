"""Index the Jellyfin library (movies + series) and watch stats into Qdrant.

Usage (two secret files: Jellyfin's own, and the shared Qdrant one —
ADR-0005, ADR-0006):
    sops exec-env ../../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run ingest.py"'
"""

import hashlib
import os

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import jellyfin_client
from textify import textify

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")
COLLECTION = "jellyfin_library"
VECTOR_SIZE = 1024  # bge-m3, see ADR-0002
ITEM_TYPES = ["Movie", "Series"]  # see ADR-0007 for why not Episode


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


def stable_id(item_id: str) -> str:
    return hashlib.sha256(item_id.encode()).hexdigest()[:32]


def main() -> None:
    client = qdrant_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}'")

    points = []
    for item_type in ITEM_TYPES:
        items = jellyfin_client.get_items(item_type)
        print(f"{len(items)} items of type '{item_type}'")
        for item in items:
            text = textify(item)
            if text is None:
                continue
            vector = embed(text)
            points.append(
                PointStruct(
                    id=stable_id(item["Id"]),
                    vector=vector,
                    payload={
                        "item_id": item["Id"],
                        "type": item_type,
                        "name": item["Name"],
                        "text": text,
                    },
                )
            )
            print(f"embedded: {text}")

    if points:
        client.upsert(collection_name=COLLECTION, points=points)
    print(f"\nIndexed {len(points)} Jellyfin items into '{COLLECTION}'")


if __name__ == "__main__":
    main()
