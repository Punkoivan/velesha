# ADR-0010: Q&A chat model — Qwen2.5-3B-Instruct, truncated retrieval context

- **Status**: accepted
- **Date**: 2026-09-19

## Context

PLAN.md Phase 2's second item: answer natural-language questions by
retrieving relevant chunks (via `cli/search.py`, ADR-0006/0009) and
having an LLM compose an answer from them (basic RAG), not just return
raw search hits. ADR-0002 already flagged this explicitly: "for real
usage hardware have to be updated or switched to some service like
voyageai" — this machine is CPU-only, and at the time of this decision
had ~700MB free RAM (~5.9GB available once cache is reclaimed), shared
with `abox` (KinD cluster), `harness-course`, and `ciso-ass` containers
(same constraint noted in ADR-0009).

## Decision

**Model**: `Qwen2.5-3B-Instruct` GGUF, Q4_K_M quant (~2GB file,
`tools/models/qwen2.5-3b-instruct-q4_k_m.gguf`) — same "small, local,
multilingual" reasoning as `bge-m3` (ADR-0002), sized to fit the
available RAM headroom rather than maximize capability. Handles
Ukrainian instructions and generation correctly in testing.

**Server**: separate `llama-server` process, port `8084` (`CHAT_URL`
env var, mirrors `EMBED_URL` from ADR-0009), `-c 4096 --jinja` — modest
context (chat model doesn't need bge-m3's 8192) and `--jinja` for the
model's own chat template (needed for `/v1/chat/completions` to format
the prompt correctly). Runs alongside the embedding server; combined
resident memory left ~4.5GB available on this machine — tight but
workable.

**Retrieval context**: `cli/ask.py` truncates each retrieved chunk to
600 characters before building the prompt (`MAX_CHUNK_CHARS`). Full
recipe texts blew the 4096-token context on the first real test (5
untruncated chunks = 4437 tokens, over the limit) — truncating chunks
was chosen over raising `-c` on the chat server, to keep memory
consumption fixed rather than trading it for a longer context window.

**Prompt**: a fixed Ukrainian system prompt instructing the model to
answer only from the given context and say so if the context doesn't
have the answer — verified in testing (asked about a recipe that
doesn't exist in the vault; model correctly said the context has no
such thing, didn't invent one).

## Alternatives considered

- **A larger/more capable local model** (e.g. Qwen2.5-7B) — better
  answers, but doesn't fit the observed RAM headroom next to the
  embedding server; revisit if this ever runs on dedicated/upgraded
  hardware, per ADR-0002's own note.
- **A cloud chat API** — same rejection as ADR-0002 for embeddings:
  sends home/life data off-machine, adds per-call cost.
- **Raise the chat server's context instead of truncating chunks** —
  would avoid losing the tail of long recipes, but costs more RAM per
  request on a machine already near its limit; truncation was the
  cheaper fix and 600 characters was enough for every question tested
  so far.

## Consequences

- Two `llama-server` processes now run for Velesha: embedding
  (`8083`, ADR-0009) and chat (`8084`). Both need to be up for
  `cli/ask.py`; `cli/search.py` only needs the embedding one.
- Long-form context (full recipe steps, not just the gist) won't reach
  the chat model — fine for "what should I cook" style questions, not
  for "what's step 4 of this recipe" without also running
  `search.py --full` separately.
- Generation is slow on CPU (~7 tokens/sec observed) — acceptable for a
  personal, occasional-use assistant, not for anything interactive at
  scale.
