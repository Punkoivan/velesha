"""Index recent Home Assistant history into Qdrant.

Usage (two secret files: HA's own, and the shared Qdrant one — ADR-0005,
ADR-0006):
    sops exec-env ../../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run ingest.py --days 3"'
"""

import argparse
import datetime
import hashlib
import os

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import ha_client
from textify import INTERESTING_DOMAINS, textify

EMBED_URL = "http://localhost:8081/embedding"
COLLECTION = "ha_history"
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


def stable_id(entity_id: str, last_changed: str) -> str:
    return hashlib.sha256(f"{entity_id}|{last_changed}".encode()).hexdigest()[:32]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3, help="history window in days")
    args = parser.parse_args()

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=args.days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    print(f"Fetching states to find entities of interest...")
    states = ha_client.get_states()
    entities = [
        s["entity_id"]
        for s in states
        if s["entity_id"].split(".", 1)[0] in INTERESTING_DOMAINS
        and "browser_mod" not in s["entity_id"]
        and "unknown" not in s["entity_id"]
    ]
    print(f"{len(entities)} entities in scope (of {len(states)} total)")

    client = qdrant_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}'")

    points = []
    for entity_id in entities:
        history = ha_client.get_history(entity_id, start_iso, end_iso)
        for event in history:
            text = textify(event)
            if text is None:
                continue
            vector = embed(text)
            points.append(
                PointStruct(
                    id=stable_id(entity_id, event["last_changed"]),
                    vector=vector,
                    payload={
                        "entity_id": entity_id,
                        "state": event.get("state"),
                        "last_changed": event["last_changed"],
                        "text": text,
                    },
                )
            )
            print(f"embedded: {text}")

    if points:
        client.upsert(collection_name=COLLECTION, points=points)
    print(f"\nIndexed {len(points)} HA events into '{COLLECTION}'")


if __name__ == "__main__":
    main()
