# API

OpenAI-compatible `/v1/chat/completions` server — Velesha as a
tool-calling agent (ADR-0018), not blind RAG. The model decides when to
call `search_knowledge` (recipes/Jellyfin/HA history), `get_live_state`
(current device/sensor state), or `get_energy_usage` (kWh delta for a
date) — instead of every question getting the same fixed context
regardless of whether it's even answerable that way. Wired into Home
Assistant via HA's built-in `llama_cpp` integration (**not** "OpenAI
Conversation" — see ADR-0013 for why).

## Running it

Needs the embedding server (8083) and a **tool-calling-capable** chat
server (8084, `--jinja`, Qwen2.5-3B-Instruct verified working) from the
repo root README already up, plus Qdrant.

```bash
uv sync
sops exec-env ../secrets.enc.env \
  'sops --config /dev/null exec-env secrets.enc.env "uv run uvicorn main:app --host 0.0.0.0 --port 8090"'
```

```bash
curl http://localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "скільки 18.09 числа пралка використала електроенергії?"}]
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

- Read-only tools only — `search_knowledge`/`get_live_state`/
  `get_energy_usage` never change anything. HA's Assist function-calling
  schema (device control, `intent__HassTurnOn` etc.) is still received
  but dropped: `conversation.velesha` can't turn things on/off. A
  device-control agent is future work.
- Any system message the caller sends (e.g. HA's own house-description
  prompt) is replaced with Velesha's own tool-aware one, not merged.
- Small-model residual: even with correct, correctly-sorted tool
  output, Qwen2.5-3B occasionally misreads which line in a list is the
  answer (see ADR-0018's Consequences) — not common, but not zero.
