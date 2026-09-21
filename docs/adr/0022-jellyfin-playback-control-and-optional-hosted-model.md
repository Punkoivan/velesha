# ADR-0022: Jellyfin playback control tool; optional hosted chat model

> Superseded by ADR-0038 (керування тепер через jellyfin-mcp з ворітьми в `adk_agent.py`).

- **Status**: accepted
- **Date**: 2026-09-19

## Context

After ADR-0021 the user asked "включи наступну серію" and got nothing
useful: the only action tool started a *title*, so the model searched for
a film called "наступну серію". Playback of what is already playing
(pause, resume, stop, next/previous episode) was not covered. The user
also concluded a stronger hosted model is now warranted — consistent with
the residual 3B-model errors documented in ADR-0018/0019/0020/0021
(wrong tool choice, misread tool output, calling an action tool on
read-only questions).

## Decision

**`control_jellyfin_playback(action)`** — `pause | resume | stop | next
| previous` on the Kodi session (same Kodi-only allowlist as ADR-0021,
same `CONTROL_TOOLS` code gate; the gate's verb list gained
пауз/продовж/зупин/стоп/наступн/попередн/далі/пропуст/next/stop/pause).
Pause/resume/stop use Jellyfin's `Playing/{Pause|Unpause|Stop}`
session commands. Next/previous are computed from the now-playing
episode's season/index against the series' sorted episode list and
started with `PlayNow` — more reliable than a generic next-track
command for a single-episode play. Edge cases return plain messages
(nothing playing, not an episode, first/last episode).

Verified on the real Kodi: pause, resume, next (S07E07 → S07E08
«Аргентина»), previous, both directly and through the agent — audit log
shows the right action for "включи наступну серію", "постав на паузу",
"продовжуй", "включи попередню серію".

**Optional hosted model.** `api/main.py` reads `CHAT_API_KEY` (and
`CHAT_MODEL`, `CHAT_URL`). Unset → the local llama-server exactly as
before. Set → requests go to an OpenAI-compatible endpoint (default
OpenAI's) with the bearer key and model name. Parameters differ by
target: the local server gets `repeat_penalty`/`temperature`/`max_tokens`
(llama.cpp extras); the hosted one gets `max_completion_tokens` only,
because hosted APIs reject unknown fields and some models reject a
non-default temperature. Tool schemas and the agent loop are unchanged
(OpenAI tool-calling format is what the local server already spoke).

## Alternatives considered

- **Extend `play_on_jellyfin_device` to take "next"** — mixes two
  different actions (start a title vs. steer current playback) behind
  one parameter; the model already confused them once.
- **Switch the code to a vendor SDK** — unnecessary while the wire
  format is OpenAI-compatible; keeps the local/hosted switch a
  configuration change.

## Consequences

- **Not verified against a hosted model**: no key was configured when
  this was written; the hosted branch is untested code until a key is
  supplied and the same question set is re-run.
- Sending questions to a hosted model sends tool *results* (device
  states, energy figures, recipe text) off the machine — the very thing
  ADR-0002 avoided for embeddings. Embeddings and Qdrant stay local; only
  the chat step would leave. A conscious trade for reliability.
- Race seen once in testing: two commands within seconds — Jellyfin's
  now-playing had not updated, so "previous" computed from the stale
  episode. Real voice commands are spaced by far more than that; not
  addressed.
