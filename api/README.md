# API

OpenAI-compatible `/v1/chat/completions` server over Velesha's RAG
pipeline (retrieval across `cli/search.py`'s collections + generation via
the local chat model) — the integration point for Home Assistant's
built-in "OpenAI Conversation" integration, or anything else that speaks
the OpenAI chat API. See ADR-0011.

## Running it

Needs the embedding server (8083) and chat server (8084) from the repo
root README already up, plus Qdrant.

```bash
uv sync
sops --config /dev/null exec-env ../secrets.enc.env \
  'uv run uvicorn main:app --host 0.0.0.0 --port 8090'
```

```bash
curl http://localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "коли я востаннє дивився мумію?"}]
}'
```

## Limitations

- No streaming — `stream: true` in the request is accepted but ignored,
  always returns a complete response. Fine for `curl`/scripted use; may
  need addressing before real HA integration if HA's client requires SSE.
- Retrieval always runs against the latest user message only, and any
  system message the caller sends is replaced with Velesha's own
  (retrieval-grounded) one — HA's own system prompt (entity states, etc.)
  isn't merged in yet. Revisit when actually wiring up HA.
