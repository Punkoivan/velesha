# ADR-0012: Embedding and chat servers stay always-on, not cron/lazy-started

- **Status**: accepted
- **Date**: 2026-09-19

## Context

With `api/` (ADR-0011) meant to be the always-reachable integration
point for Home Assistant, the question came up: does the embedding
server (`8083`) — and by extension the chat server (`8084`) — need to
run continuously, or could it be started on demand (a cron job for
periodic re-indexing, or a lazy-start-with-idle-timeout supervisor for
the query path)?

The two request paths have different shapes:
- **Ingestion** (`*/ingest.py`, `sources/*/index.py`) is a batch job,
  invoked by hand or on a schedule — a genuinely good fit for
  start-server → run → stop-server, cron or otherwise.
- **Query** (`cli/search.py`, `cli/ask.py`, and `api/`'s
  `/v1/chat/completions`) needs to answer an unpredictable request (a
  person, or HA's Assist pipeline) with no advance notice — a cron
  schedule can't anticipate that, and a lazy-start supervisor would add
  ~2s cold-start latency to whichever request happens to be first after
  an idle period, plus the supervisor itself as new code to maintain.

## Decision

Keep both `llama-server` processes (embedding, chat) running
continuously, exactly as already set up (ADR-0009, ADR-0010). No
lazy-start, no idle-timeout supervisor, no cron-gated startup for the
query path.

## Alternatives considered

- **Lazy-start with idle-timeout** — would reclaim RAM (~1.5-3GB per
  process) between active use, at the cost of cold-start latency on the
  first request after idle and a new supervisor component to write and
  keep correct (race conditions between "starting up" and "another
  request arrives" are the usual failure mode for this pattern).
  Deliberately deferred, not ruled out — worth revisiting if RAM pressure
  from other processes on this shared machine (`abox`, `harness-course`,
  `ciso-ass`) becomes a recurring practical problem rather than a
  one-time observation (ADR-0009/0010).
- **Cron-only for ingestion, leave query servers as a separate always-on
  concern** — this is actually already true regardless of this ADR:
  `ingest.py` scripts can be cron-scheduled independently any time,
  since they only need the embedding server up for the duration of their
  own run. Not blocked by this decision either way.

## Consequences

- Steady-state RAM cost: embedding (8083) + chat (8084) + API (8090)
  servers all resident continuously, same footprint documented in
  ADR-0009/0010/0011.
- Query latency (`cli/search.py`, `cli/ask.py`, `api/`) has no cold-start
  tax — every request hits an already-warm model.
- If this machine's memory pressure gets worse, the lazy-start option
  above is the one to build, not a rewrite of this decision's reasoning.
