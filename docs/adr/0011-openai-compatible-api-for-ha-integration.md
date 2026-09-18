# ADR-0011: OpenAI-compatible API server as the Phase 3 integration point

- **Status**: accepted
- **Date**: 2026-09-19

## Context

Phase 3's "CLI tool proper" item was originally framed as a nicer CLI.
But the actual near-term goal that came up in conversation is
integrating Velesha into Home Assistant — HA ships a built-in "OpenAI
Conversation" integration that can point at any OpenAI-compatible
`base_url` for its chat model, including a self-hosted one. Pointing HA
directly at the chat `llama-server` from ADR-0010 would work for plain
chat, but loses retrieval entirely — the model would answer from
training data / conversation only, none of Velesha's actual indexed
data (recipes, HA history, Jellyfin).

A CLI script (`cli/ask.py`) can't be pointed at by HA at all — HA needs
something reachable over HTTP, staying up continuously, not a one-shot
process.

## Decision

Add `api/`: a small FastAPI server exposing `/v1/chat/completions`
(OpenAI-compatible) and `/v1/models`, wrapping the same retrieve-then-
generate logic as `cli/ask.py` (duplicated here rather than imported —
consistent with every other source's pattern of small, independent
scripts, not a shared library). On each request: embed the latest user
message, retrieve + truncate context from all three collections (same
as ADR-0010), build an augmented system prompt, forward to the chat
`llama-server` (ADR-0010), return the response in OpenAI's shape.

This becomes the thing HA (or anything else OpenAI-API-shaped) points
at — `cli/ask.py` stays as the direct-terminal way to ask questions,
`api/` is the always-on integration surface. Runs on port `8090` for
now, no auth (matches this being a personal-network-only service, same
posture as the embedding/chat `llama-server`s themselves).

## Alternatives considered

- **A general-purpose CLI framework** (Click/Typer polish over the
  existing scripts) — doesn't actually enable HA integration, which is
  the concrete near-term goal; deferred, not rejected — still may be
  worth doing for ergonomics later.
- **Point HA straight at the chat `llama-server`** — simplest, zero new
  code, but throws away the entire point of Phase 1/2 (Velesha's actual
  memory of home/life data) for the sake of skipping one wrapper.
- **A shared Python module for the retrieve+generate logic**, imported by
  both `cli/ask.py` and `api/main.py` — more correct DRY-wise, but each
  directory in this repo is its own `uv` project with its own venv
  (established pattern since ADR-0002); a shared importable module would
  need its own packaging story. Not worth it yet for ~70 lines of
  duplicated logic; revisit if the two ever drift and cause a real bug.

## Consequences

- A third long-running process joins the embedding (8083) and chat
  (8084) `llama-server`s: the API server itself (8090, lightweight —
  FastAPI/Uvicorn, no model weights of its own).
- Not yet wired into HA — this ADR covers building the integration
  *point*, not the integration itself (adding the "OpenAI Conversation"
  integration in HA's UI, pointed at `http://<this-host>:8090/v1`, is a
  follow-up step once this box's own network reachability from HA is
  confirmed).
- No streaming support and no merging of a caller-supplied system prompt
  with Velesha's own (see `api/README.md`) — both are real gaps to close
  before HA's Assist pipeline can use this for anything beyond simple
  one-shot Q&A; not blocking to stand the server up and test it directly.
