# Velesha (Велеша)

A personal assistant with real memory of home and life data — Home
Assistant history, Jellyfin media library, Obsidian notes and recipes, and
whatever else earns a place. Named after Veles, the Slavic god of wisdom
and knowledge. Search-based today, voice + avatar planned.

See [`PLAN.md`](PLAN.md) for the roadmap and [`docs/adr/`](docs/adr/) for
why things are built the way they are — start with
[ADR-0001](docs/adr/0001-project-name-and-mission.md).

## Status

Phase 1 (Memory): Obsidian recipes and Home Assistant history indexed and
searchable. See [`sources/obsidian/`](sources/obsidian/) and
[`sources/home_assistant/`](sources/home_assistant/). Jellyfin next.

## Stack

- Embeddings: local `llama-server` (llama.cpp) + `bge-m3`, multilingual —
  [ADR-0002](docs/adr/0002-local-embeddings-via-llamacpp.md)
- Vector store: Qdrant, one collection per source —
  [ADR-0003](docs/adr/0003-qdrant-as-vector-store.md)
- Secrets: SOPS + age, encrypted files committed, no plaintext `.env` —
  [ADR-0005](docs/adr/0005-secrets-via-sops-age.md)

## Running it

```bash
# 1. Embedding server
cd tools/llama.cpp
./build/bin/llama-server -m ../models/bge-m3-Q4_K_M.gguf --embedding --pooling cls \
  -c 8192 -b 8192 -ub 8192 --port 8081 --host 127.0.0.1

# 2. Qdrant reachable at localhost:6333 (currently: port-forward into the
#    abox cluster — see ADR-0003)

# 3. Index / search a source
cd sources/obsidian
uv run index.py
uv run search.py "щось із куркою на вечерю"
```
