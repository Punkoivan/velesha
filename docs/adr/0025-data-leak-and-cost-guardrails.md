# ADR-0025: Data-leak and cost guardrails for hosted models

- **Status**: accepted
- **Date**: 2026-09-19

## Context

With a hosted model on the way (ADR-0022) and an agentgateway planned in
front of it, the user named the two risks that matter: **data leakage**
and **cost**. What leaves the machine on a hosted call is not just the
question but also the *tool results* the agent fed back (door states,
counters, torrent names, recipes) and the tool schemas.

The privacy line was clarified in two rounds (first answer misread, then
corrected by the user): there are **three tiers of destination**, not
one "cloud":

| Tier | Sees | Why |
|------|------|-----|
| Local llama-server | everything | nothing leaves the machine |
| Paid API (OpenAI) | everything, after secret masking, incl. home state and history | the main agent model; paid API data is not used to improve products by default — **to verify in the provider's terms** |
| **Free Gemini** (planned lane) | only tool-less general questions — **never any tool result** | Google states free-tier content is used to improve its products |

A first implementation blocked home-state/history from *all* hosted
models; the user's correction ("paid providers are fine, the free one is
not") made that a configurable option rather than the default.

## Decision

`api/guardrails.py`, applied inside our process before any hosted call —
an agentgateway later is a second layer, not a replacement. Local calls
are never touched.

**Leak controls (hosted calls):**
- **Always-on masking** of everything sent: the exact values of every
  secret-looking env var (`*TOKEN*/*KEY*/*PASSWORD*/*SECRET*`), the
  hostnames of our own services (from `*_URL` vars), JWTs, `sk-…` keys,
  `user:pass@` in URLs, private IP ranges, emails, and 32+-char hex ids
  (API keys, user ids, torrent hashes). Applied to message contents and
  to tool-call arguments.
- **Data-class routing, opt-in:** `HOSTED_BLOCKED_TOOLS` (default empty —
  the paid tier may see everything). Results of listed tools are never
  sent to a hosted model; from that point the *rest of the request* is
  finished by the local model. Built and tested, kept as the mechanism
  the free-Gemini lane and any future stricter policy will use.
- **Free lane rule (to implement with that lane):** a structural check
  that the payload has no `tools` and no `role: tool` message — enforced
  in code, not by prompt (prompt-only gating already failed twice, see
  ADR-0021).

**Cost controls:**
- **Daily token budget** (`DAILY_TOKEN_BUDGET`, default 1 000 000 — raised
  from an initial 500 000 the same day at the user's call after ~500k
  proved to be ≈30–50 tool-using questions and testing shares the counter;
  the user observed the day's spend at about 20 cents), counted from the provider's
  `usage.total_tokens`, persisted in `api/data/usage.json` (gitignored,
  older days dropped). When exhausted the agent uses the local model
  instead of failing.
- **Hosted failure → local** (HTTP ≥ 400 or network error).
- **Loop stop:** an identical tool call (name + arguments) twice in one
  request ends the run; together with `MAX_TOOL_ITERATIONS = 4` and
  `max_completion_tokens = 600` this bounds a runaway agent.
- **Audit lines** per step: `LLM hosted|local`, `TOOL name args`.

## Verification

Against a stub "hosted" server that records exactly what it receives
(no real key, no spend), 11/11 checks passed: HA token, Jellyfin key,
`192.168.*` address, email, JWT and our own hostname absent from
everything sent, placeholders present; budget accounting exact and an
exhausted budget sends nothing hosted (local answered); a hosted 500
falls back to local; an identical repeated call stops the agent; with
`get_live_state` blocked the hosted model saw one call only, no door
state in what it saw, and the local model finished the answer.

## Alternatives considered

- **Do it only in the gateway** — a gateway can mask and rate-limit
  traffic but cannot see which tool a result came from, so data-class
  routing needs our process anyway; and a gateway that is down or
  misconfigured must not silently remove the protection.
- **USD budget instead of tokens** — needs a price table that goes
  stale per model; tokens are exact, price can be added at the gateway.
- **Block by default** — rejected by the user for paid providers.

## Consequences

- Masking is pattern-based: a secret in an unusual format that is not in
  the environment can pass through. Conversation *content* (recipes,
  titles) is not masked.
- The paid tier sees home state and history; if that ever needs to
  change, it is one env var (`HOSTED_BLOCKED_TOOLS=get_live_state,…`).
- The budget is process-wide, not per user — fine for a single-user
  assistant.
- Next: the free-Gemini lane (with the structural check), the model
  comparison, and agentgateway as the second layer (prompt guards for
  masking on the wire, route-level limits).
