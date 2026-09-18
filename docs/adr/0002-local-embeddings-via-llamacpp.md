# ADR-0002: Local embeddings via llama.cpp + bge-m3

- **Status**: accepted
- **Date**: 2026-09-18

## Context

Every source (HA history, Jellyfin metadata, Obsidian notes/recipes) needs
to be turned into vectors before it can be searched semantically. Content is
mixed-language but mostly Ukrainian, and this runs on a personal machine —
no interest in per-call cost or sending home/life data to a third-party API.

## Decision

Run embeddings locally via `llama-server` (llama.cpp) in `--embedding` mode,
using `bge-m3` (GGUF, Q4_K_M quant, ~440MB) as the model. `bge-m3` is
multilingual and handles Ukrainian well, which rules out English-only models
like most `bge-small`/`e5-small` variants.

Server config that matters: `-b 8192 -ub 8192` to match context size —
the default `n_ubatch` (512) rejects any input longer than ~512 tokens with
a 500 error, which is easy to hit on a full recipe or a long note.

## Alternatives considered

- **Cloud embedding API** (OpenAI, Cohere, etc.) — simpler to start, but
  sends personal home/life data off-machine on every ingest and adds
  per-token cost for something that runs repeatedly during development.
- **sentence-transformers directly in Python** — avoids running a server,
  but loses llama.cpp's quantization (smaller footprint, faster on CPU) and
  the reusable local inference server that other parts of Velesha (chat,
  later voice) will also want.

## Consequences

- All ingestion scripts depend on `llama-server` running on
  `localhost:8081` before they run.
- Model files and the llama.cpp build are gitignored (too large, and
  trivially reproducible from `scripts/setup.sh` — see ADR-0004 once that
  script exists).
- Vector dimension is fixed at 1024 (bge-m3 output size) — every collection
  in the vector store must agree on this.
