# ADR-0009: Run Velesha's own embedding server, not a borrowed one; make the port configurable

- **Status**: accepted
- **Date**: 2026-09-19

## Context

While working on Phase 2's unified search CLI, the `llama-server` every
Velesha script was actually calling on `localhost:8081` turned out to be
running from a different project on this machine
(`harness-course/tools/llama.cpp`, same `bge-m3-Q4_K_M.gguf` model), not
from this repo's own `tools/llama.cpp`. Same shape of problem as
ADR-0006 (Qdrant on `abox`): a working dependency on a process this
project doesn't own or control, on a machine that also runs `abox`
(KinD cluster), `harness-course`, and `ciso-ass` containers — any of
which can be torn down independently of Velesha.

The machine is also memory-constrained (observed: ~700MB free of 30GB,
~5.4GB available with cache reclaimed, no GPU) — relevant context for
any future decision about running an LLM here (e.g. Phase 2's Q&A item),
even though this ADR itself only concerns the embedding server.

## Decision

Run the embedding server from this repo's own `tools/llama.cpp` build
(binary already present at `tools/llama.cpp/build/bin/llama-server`),
against `tools/models/bge-m3-Q4_K_M.gguf` — same model, same config, just
Velesha's own process. Needs `LD_LIBRARY_PATH=./build/bin` (the build's
shared libs, e.g. `libllama-server-impl.so`, aren't installed system-wide).

Port moved from the hardcoded `8081` (occupied by the foreign process) to
`8083`, and every script now reads it from an `EMBED_URL` environment
variable with `http://localhost:8083/embedding` as the default — not
hardcoded, so a different port (or a remote embedding server later) is a
one-variable change, not a find-and-replace across every source.

The foreign `harness-course` process on `8081` was left running,
untouched — killing another project's process isn't this project's call
to make unilaterally on a shared machine.

## Alternatives considered

- **Keep using the foreign process, just document the dependency** —
  rejected for the same reason as ADR-0006: a working dependency on
  infrastructure this project doesn't control is exactly the failure
  mode that already bit Velesha once (`abox`).
- **Reuse port 8081 by stopping the foreign process** — simpler (no code
  changes needed), but stopping another project's process to free the
  port isn't a call to make without that project's owner's say-so, even
  though it happens to be the same user.

## Consequences

- `tools/llama.cpp` must actually be built (`cmake --build`) before
  Velesha's embedding server can run — previously true anyway since the
  binary was already present, just never confirmed as *this* repo's own
  build being used.
- Every source's `EMBED_URL` is now late-bound via environment, matching
  how `QDRANT_URL`/`QDRANT_API_KEY` already work (ADR-0006) — consistent
  pattern across all connection details.
- Anyone running Velesha fresh needs the `LD_LIBRARY_PATH` gotcha from
  this ADR, not just the plain `llama-server -m ...` command ADR-0002
  originally documented.
