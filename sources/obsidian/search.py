"""Semantic search over indexed Obsidian recipes.

Usage:
    uv run search_recipes.py "щось із куркою на вечерю"
"""

import sys

import requests
from qdrant_client import QdrantClient

EMBED_URL = "http://localhost:8081/embedding"
QDRANT_URL = "http://localhost:6333"
COLLECTION = "obsidian_recipes"


def embed(text: str) -> list[float]:
    resp = requests.post(EMBED_URL, json={"content": text}, timeout=60)
    resp.raise_for_status()
    embedding = resp.json()[0]["embedding"]
    if isinstance(embedding[0], list):
        embedding = embedding[0]
    return embedding


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run search_recipes.py <query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    client = QdrantClient(url=QDRANT_URL)
    vector = embed(query)

    results = client.query_points(
        collection_name=COLLECTION, query=vector, limit=5, with_payload=True
    ).points

    for r in results:
        print(f"{r.score:.3f}  {r.payload['title']}")


if __name__ == "__main__":
    main()
