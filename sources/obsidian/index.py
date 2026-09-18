"""Index Obsidian recipes into Qdrant using a local llama.cpp embedding server.

Usage:
    uv run index_recipes.py
"""

import hashlib
import pathlib
import sys

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

RECIPES_DIR = pathlib.Path("/home/punka/obsidian/Домашнє/рецепти")
EMBED_URL = "http://localhost:8081/embedding"
QDRANT_URL = "http://localhost:6333"
COLLECTION = "obsidian_recipes"
VECTOR_SIZE = 1024  # bge-m3 output dimension


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
    client = QdrantClient(url=QDRANT_URL)

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
