# Velesha (Велеша)

A personal assistant with real memory of home and life data — Home
Assistant history, Jellyfin media library, Obsidian notes, Tandoor
recipes, and whatever else earns a place. Named after Veles, the Slavic
god of wisdom and knowledge. Search-based today, voice + avatar planned.

See [`PLAN.md`](PLAN.md) for the roadmap and [`docs/adr/`](docs/adr/) for
why things are built the way they are — start with
[ADR-0001](docs/adr/0001-project-name-and-mission.md).

## Status

Phase 1 (Memory) and Phase 2 (Recall) done: Home Assistant history, the
Jellyfin library (movies + series watch stats), and Tandoor recipes
(primary recipe source since ADR-0015 — Obsidian recipes migrated in)
are indexed and searchable on the `ha-addon-qdrant` instance, with a
unified search CLI (`cli/search.py`) and RAG Q&A (`cli/ask.py`) across
all three active collections. See
[`sources/home_assistant/`](sources/home_assistant/),
[`sources/jellyfin/`](sources/jellyfin/),
[`sources/tandoor/`](sources/tandoor/),
[`sources/obsidian/`](sources/obsidian/) (dormant), and [`cli/`](cli/).

Phase 3 (interface): `api/` is wired into Home Assistant as
`conversation.velesha` (via HA's built-in `llama_cpp` integration — see
[ADR-0013](docs/adr/0013-ha-llama-cpp-integration-streaming-required.md)).
It's a tool-calling agent, not blind RAG — see
[ADR-0018](docs/adr/0018-tool-calling-agent-replaces-blind-rag.md) — so
it can search indexed knowledge, check a device's live state, or
compute energy usage, deciding per-question which (if any) it needs.
Voice/avatar not started — see [`PLAN.md`](PLAN.md).

## Stack

- Embeddings: local `llama-server` (llama.cpp) + `bge-m3`, multilingual —
  [ADR-0002](docs/adr/0002-local-embeddings-via-llamacpp.md)
- Vector store: Qdrant, one collection per source, running as the
  `ha-addon-qdrant` Home Assistant add-on (durable, backed up with HA
  itself — not `abox`, which is ephemeral and got rebuilt from under this
  project once already) —
  [ADR-0003](docs/adr/0003-qdrant-as-vector-store.md),
  [ADR-0006](docs/adr/0006-qdrant-off-abox-onto-ha-addon.md)
- Secrets: SOPS + age, encrypted files committed, no plaintext `.env` —
  [ADR-0005](docs/adr/0005-secrets-via-sops-age.md)
- Q&A: local `llama-server` + `Qwen2.5-3B-Instruct`, retrieval-grounded —
  [ADR-0010](docs/adr/0010-qa-chat-model-qwen25-3b-truncated-context.md)

## Running it

```bash
# 1. Embedding server — must be this repo's own build (tools/llama.cpp),
#    not one borrowed from another project (see ADR-0009). Default port
#    is 8083, overridable via EMBED_URL in every script.
cd tools/llama.cpp
LD_LIBRARY_PATH=./build/bin ./build/bin/llama-server \
  -m ../models/bge-m3-Q4_K_M.gguf --embedding --pooling cls \
  -c 8192 -b 8192 -ub 8192 --port 8083 --host 127.0.0.1

# 2. Qdrant: ha-addon-qdrant, reached over TLS (its cert is a real Let's
#    Encrypt cert, no custom CA needed — see ADR-0006). Connection details
#    live in the shared secret at the repo root, decrypted only into a
#    subprocess's environment:

# 3. Index / search a source (each source's secrets.enc.env is chained
#    after the shared one when the source also needs its own secret, e.g.
#    Tandoor's own API token). Tandoor is the primary recipe source
#    (ADR-0015) — sources/obsidian is dormant.
cd sources/tandoor
sops exec-env ../../secrets.enc.env 'sops --config /dev/null exec-env secrets.enc.env "uv run ingest.py"'
sops exec-env ../../secrets.enc.env 'uv run search.py "щось із куркою на вечерю"'

# 4. Or search everything at once (Phase 2, cli/search.py):
cd cli
sops --config /dev/null exec-env ../secrets.enc.env 'uv run search.py "щось із куркою на вечерю"'

# 5. Chat server, for Q&A (ADR-0010) — separate process/port, only
#    needed for cli/ask.py and api/, not for search. -c 8192, not 4096
#    (ADR-0016 — per-collection retrieval needs the bigger context):
cd tools/llama.cpp
LD_LIBRARY_PATH=./build/bin ./build/bin/llama-server \
  -m ../models/qwen2.5-3b-instruct-q4_k_m.gguf -c 8192 --jinja \
  --port 8084 --host 127.0.0.1

cd cli
sops --config /dev/null exec-env ../secrets.enc.env 'uv run ask.py "коли я востаннє дивився мумію?"'

# 6. API server / agent, for HA integration (ADR-0011, ADR-0013, ADR-0018)
#    — needs its own HA secret too (tool calls live HA state/history):
cd api
sops exec-env ../secrets.enc.env \
  'sops --config /dev/null exec-env secrets.enc.env "uv run uvicorn main:app --host 0.0.0.0 --port 8090"'
# Then in HA: add the "llama.cpp" integration, base_url
# http://<this-host-LAN-IP>:8090/v1, no API key — see api/README.md.
```
