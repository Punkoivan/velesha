"""Grocy recipes → Qdrant `grocy_recipes`, kept in sync by a background loop (ADR-0063).

Replaces the one-shot Tandoor ingest (sources/tandoor): Tandoor is retired
and Grocy recipes change — via Velesha (recipe_add) or by hand in Grocy's
UI — so a static snapshot would go stale. Every _INTERVAL the loop
re-reads Grocy, embeds only recipes whose text changed (sha256 in the
payload) and deletes points of recipes that no longer exist.
"""

import asyncio
import hashlib
import html
import re

from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams

import grocy_client
from search_backend import embed, qdrant_client

COLLECTION = "grocy_recipes"
VECTOR_SIZE = 1024  # bge-m3, ADR-0002
_INTERVAL = 600
_MAX_EMBED_CHARS = 4000  # keeps one long recipe within the embedding server's context


def _plain(text: str) -> str:
    """Grocy's UI editor saves HTML; recipes imported from Tandoor are plain text."""
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def textify(recipe: dict, ingredients: list[str]) -> str:
    parts = [recipe["name"]]
    if ingredients:
        parts.append("Інгредієнти: " + ", ".join(ingredients))
    if desc := _plain(recipe.get("description") or ""):
        parts.append(desc)
    return "\n".join(parts)


def point_id(recipe_id: int) -> str:
    return hashlib.sha256(f"grocy-recipe:{recipe_id}".encode()).hexdigest()[:32]


def sync() -> tuple[int, int]:
    """Returns (re-embedded, deleted)."""
    client = qdrant_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(COLLECTION, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE))

    products = {p["id"]: p["name"] for p in grocy_client.objects("products")}
    by_recipe: dict[int, list[str]] = {}
    for pos in grocy_client.objects("recipes_pos"):
        if name := products.get(pos["product_id"]):
            by_recipe.setdefault(pos["recipe_id"], []).append(name)
    recipes = [r for r in grocy_client.objects("recipes") if r.get("type") == "normal"]

    indexed = {}
    offset = None
    while True:
        pts, offset = client.scroll(COLLECTION, limit=256, offset=offset, with_payload=["recipe_id", "sha"])
        indexed.update({p.id.replace("-", ""): p.payload.get("sha") for p in pts})
        if offset is None:
            break

    changed = 0
    for r in recipes:
        text = textify(r, by_recipe.get(r["id"], []))
        sha = hashlib.sha256(text.encode()).hexdigest()
        if indexed.get(point_id(r["id"])) == sha:
            continue
        # one upsert per recipe: embedding is slow on CPU (~seconds each), an
        # interrupted first run shouldn't throw away what's already embedded
        client.upsert(COLLECTION, points=[PointStruct(
            id=point_id(r["id"]), vector=embed(text[:_MAX_EMBED_CHARS]),
            payload={"title": r["name"], "recipe_id": r["id"], "text": text, "sha": sha})])
        changed += 1

    gone = set(indexed) - {point_id(r["id"]) for r in recipes}
    if gone:
        client.delete(COLLECTION, points_selector=PointIdsList(points=list(gone)))
    return changed, len(gone)


async def loop() -> None:
    while True:
        try:
            changed, deleted = await asyncio.to_thread(sync)
            if changed or deleted:
                print(f"recipe index: {changed} re-embedded, {deleted} deleted", flush=True)
        except Exception as e:  # Grocy/Qdrant/embedding down — try again next round
            print(f"recipe index error: {e}", flush=True)
        await asyncio.sleep(_INTERVAL)
