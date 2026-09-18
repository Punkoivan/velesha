# ADR-0013: HA integration is the built-in `llama_cpp` integration, not "OpenAI Conversation"; streaming is mandatory

- **Status**: accepted
- **Date**: 2026-09-19

## Context

ADR-0011 planned to wire `api/` into Home Assistant via HA's built-in
"OpenAI Conversation" integration, pointed at a custom `base_url`. That
assumption turned out to be wrong for this HA version (2026.9.2): the
integration's config flow only asks for an `api_key` (verified via its
config-flow API directly, with `show_advanced_options` both on and off)
and the official docs confirm why — "This integration works only with
the official OpenAI API endpoint and does not support OpenAI-API-
compatible third-party services, proxies, or alternative backends."

An `extended_openai_conversation` custom (HACS) integration was already
present on this HA instance from earlier, and does support a custom
`base_url` — but its config entry was in a broken `not_loaded` state
with `supports_unload`/`supports_reconfigure` both `false`, meaning the
integration's own code is no longer installed on the host (only a stale
config entry remains). No SSH/file access to the HA host was available
in this session to reinstall it via HACS.

The user found the fix: HA ships a **separate**, unrelated built-in
integration, `llama_cpp`, purpose-built for exactly this — "allows you
to use a local or remote server that implements the OpenAI-compatible
chat completions API as a conversation agent," with `base_url` and
optional `api_key` as its only config fields. Verified present and
working on this HA instance via its config-flow API.

## Decision

Use HA's built-in `llama_cpp` integration, not "OpenAI Conversation."
Set up via HA's config-entries flow API (`POST
/api/config/config_entries/flow` with `handler: "llama_cpp"`), giving:

- `base_url`: `http://192.168.88.224:8090/v1` (this machine's LAN IP —
  `api/` binds `0.0.0.0`, confirmed reachable from the HA host)
- `api_key`: empty (matches `api/`'s no-auth posture, ADR-0011)
- `chat_model`: `velesha` (the one entry `api/`'s `/v1/models` reports)

This created config entry `01M2V8S1TEYFD92KKMEY09Q1TC`, `state: loaded`,
with a `conversation` subentry named "Velesha" —
`conversation.velesha` in HA.

**Streaming had to be implemented for real**, not left as the "ignored"
stub ADR-0011 shipped with. First live test via
`/api/conversation/process` failed with `"Unable to get response"` even
though `api/`'s own log showed a clean `200 OK` — debug logging of the
raw request body showed why: the `llama_cpp` integration always sends
`"stream": true`, and expects an SSE (`text/event-stream`) response, not
a single JSON blob. `api/main.py` now proxies `stream: true` requests
straight through to the chat `llama-server` (which already speaks SSE
natively) via `requests.post(..., stream=True)` +
`StreamingResponse(media_type="text/event-stream")`, forwarding raw
bytes unchanged rather than re-framing them — simplest correct option,
and the chat completion's `model` field inside each chunk stays whatever
the upstream `llama-server` reports, not overridden to `"velesha"` (a
cosmetic detail, not fixed).

Verified end-to-end via `POST /api/conversation/process` with
`agent_id: conversation.velesha`: both a small-talk message and a real
retrieval question ("порадь щось із куркою на вечерю") returned
`response_type: "action_done"` with a correct, retrieval-grounded
Ukrainian answer.

## Alternatives considered

- **Fix `extended_openai_conversation` via HACS** — would have kept the
  `base_url`-capable custom integration the user already had, but needs
  either the HA UI (HACS reinstall) or host file access, neither
  available in this session; also strictly redundant once `llama_cpp`
  turned out to cover the same need as a built-in, no-HACS-dependency
  integration.
- **Fake/single-chunk "streaming"** (send the whole answer as one SSE
  event instead of proxying the upstream's real token stream) — would
  have satisfied the protocol without touching `stream_upstream`'s
  design, but proxying the real thing was no harder and gives actual
  incremental output for any future consumer that renders it
  progressively (voice UI, a chat frontend).

## Consequences

- `api/`'s `tools` field from HA's request (the full Assist function-
  calling schema — `HassTurnOn`, `HassTurnOff`, `GetLiveContext`, etc.)
  is still received but silently dropped, same as ADR-0011's original
  design — `conversation.velesha` can answer questions grounded in
  Velesha's indexed data, but **cannot control devices or answer
  live-state questions** ("is the light on?"); HA's own system prompt
  describing the house is also dropped, replaced by Velesha's retrieval-
  augmented one. A real device-control agent is future work, not this
  ADR's scope.
- Non-streaming (`stream: false`) callers (`curl`, `cli/ask.py`-style
  testing) still work unchanged — only `stream: true` takes the new
  path.
- HA's own request timeout is tight enough that a cold/contended chat
  `llama-server` (e.g. right after several back-to-back requests during
  setup) can still time out — observed once during setup, resolved on
  retry once the server's request queue drained. Not addressed
  structurally; a slow first response after idle is a known tradeoff of
  staying always-on-but-single-model (ADR-0012) rather than something
  this ADR fixes.
