# API

OpenAI-compatible `/v1/chat/completions` server over Velesha's RAG
pipeline (retrieval across `cli/search.py`'s collections + generation via
the local chat model) — wired into Home Assistant via HA's built-in
`llama_cpp` integration (**not** "OpenAI Conversation" — see ADR-0013 for
why). Works with any OpenAI-chat-API client, streaming or not.

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

## HA setup

Add the **`llama_cpp`** integration (Settings → Devices & Services → Add
Integration → "llama.cpp") — not "OpenAI Conversation", which dropped
custom-`base_url` support entirely (ADR-0013). Point it at
`http://<this-host-LAN-IP>:8090/v1`, leave the API key empty, pick the
`velesha` model it discovers. Creates a `conversation.<name>` entity
usable via `/api/conversation/process` or HA's Assist pipeline.

## Limitations

- HA's Assist function-calling schema (`tools` in the request — device
  control, live entity state) is received but dropped: `conversation.velesha`
  answers retrieval-grounded questions, it does **not** control devices
  or answer "what's the current state of X" questions. A real
  device-control agent is future work.
- Any system message the caller sends (e.g. HA's own house-description
  prompt) is replaced with Velesha's own retrieval-augmented one, not
  merged.
