"""OpenAI-compatible chat API over Velesha's RAG pipeline.

A long-running HTTP server, not a one-shot script — this is what Home
Assistant's built-in "OpenAI Conversation" integration (or anything else
speaking the OpenAI chat API) can point at directly, instead of pointing
at the chat model's own llama-server and losing retrieval. See ADR-0011.

Same retrieve-then-generate logic as cli/ask.py, wrapped as a server
instead of a CLI invocation.

Usage:
    sops exec-env ../secrets.enc.env \\
      'uv run uvicorn main:app --host 0.0.0.0 --port 8090'
"""

import os
import time
import uuid

import requests
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8083/embedding")
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8084/v1/chat/completions")

COLLECTIONS = ["obsidian_recipes", "ha_history", "jellyfin_library"]
CHUNKS_PER_QUERY = 5
MAX_CHUNK_CHARS = 600  # see ADR-0010 — keeps the chat model's context from overflowing

SYSTEM_PROMPT = (
    "Ти — Велеша, персональний асистент. Відповідай українською, коротко "
    "і по суті, спираючись ТІЛЬКИ на наданий контекст. Якщо в контексті "
    "немає відповіді — так і скажи, не вигадуй."
)

app = FastAPI(title="Velesha API")


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


def retrieve_context(query: str) -> str:
    client = qdrant_client()
    vector = embed(query)

    hits = []
    for collection in COLLECTIONS:
        if not client.collection_exists(collection):
            continue
        results = client.query_points(
            collection_name=collection, query=vector, limit=CHUNKS_PER_QUERY, with_payload=True
        ).points
        for r in results:
            hits.append({"collection": collection, "score": r.score, "payload": r.payload})
    hits.sort(key=lambda h: h["score"], reverse=True)

    lines = []
    for h in hits[:CHUNKS_PER_QUERY]:
        text = " ".join(h["payload"].get("text", "").split())[:MAX_CHUNK_CHARS]
        lines.append(f"[{h['collection']}] {text}")
    return "\n".join(lines)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "velesha"
    messages: list[ChatMessage]
    max_tokens: int = 400
    temperature: float = 0.2
    stream: bool = False


def stream_upstream(payload: dict):
    with requests.post(CHAT_URL, json=payload, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=1024):
            if chunk:
                yield chunk


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "velesha", "object": "model", "owned_by": "velesha"}]}


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    user_messages = [m for m in req.messages if m.role == "user"]
    question = user_messages[-1].content if user_messages else ""

    context = retrieve_context(question)
    augmented_system = f"{SYSTEM_PROMPT}\n\nКонтекст:\n{context}" if context else SYSTEM_PROMPT

    upstream_messages = [{"role": "system", "content": augmented_system}]
    upstream_messages += [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]

    payload = {
        "messages": upstream_messages,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }

    if req.stream:
        return StreamingResponse(stream_upstream({**payload, "stream": True}), media_type="text/event-stream")

    resp = requests.post(CHAT_URL, json=payload, timeout=120)
    resp.raise_for_status()
    upstream = resp.json()

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "velesha",
        "choices": upstream["choices"],
        "usage": upstream.get("usage", {}),
    }
