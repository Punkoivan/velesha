# ADR-0019: `get_live_state` returns near-tie candidates; hardening against 3B-model noise

- **Status**: accepted
- **Date**: 2026-09-19

## Context

User asked whether the agent can report on other HA entities, e.g. the
AdGuard Home add-on. It exposes ~14 entities: `switch.*` toggles
(protection, filtering, safe search...) and `sensor.*` counters (DNS
queries, blocked queries, blocked ratio, processing speed). Because
`get_live_state` (ADR-0018) reads any domain, no new tool was needed —
but testing exposed four separate problems, all around how a small
model consumes tool output:

1. **My own prompt wording blocked the tool.** I had written
   `get_live_state` was "not for 'скільки за день'" questions; asked
   "скільки DNS-запитів заблокував AdGuard?" the model refused to call
   it at all. Reworded: current value of any device/sensor/*counter*,
   not past events or per-date sums.
2. **Silent single-entity resolution picked wrong entities.** A bare
   hint matched many AdGuard entities; the winner was arbitrary
   (answered "13470 blocked" — the *total* counter; another run
   answered "0" — parental-control blocked). Fixed in two parts:
   word-overlap scoring (handles reordered/dropped words in long
   names) and returning near-tie candidates (score within 0.1 of best,
   max 5) with their states so the model picks. Returning a fixed 5
   always was tried first and made things worse — with five loosely
   related lines the 3B model started narrating unrelated entities
   (battery level, the fridge) — hence the near-tie cut.
3. **Raw HA state strings confused the model.** `on`/`off` came back as
   English tokens; a switch question was answered with an unrelated
   counter, and a door sensor's `off` was read as "вимкнено"
   (turned off) instead of "closed". Tool output now translates states
   to Ukrainian, device-class-aware: opening sensors →
   відчинено/закрито, motion → рух є/руху немає, else
   увімкнено/вимкнено. The entity-hint parameter description now asks
   for a specific name ("AdGuard Захист", not just "AdGuard").
4. **`llama-server` returns HTTP 500 when the model's output fails its
   tool-call parser** (garbled byte tokens from the quantized model,
   seen once mid-answer). `run_agent` retries once (sampling isn't
   deterministic) and otherwise returns a plain Ukrainian "спробуй
   перефразувати" instead of surfacing a 500 to HA. The device's
   battery sensor (`sensor.unknown_batareia`, transliterated id) is
   also added to the noise filter — it was pulling answers off-topic.

Verified: "скільки DNS-запитів заблокував AdGuard?" → 481 (correct, all
runs); "який відсоток ... блокує AdGuard?" → ~3.56%; "чи увімкнений
захист в AdGuard?" → active; front-door state correct 2 of 3 runs.

## Decision

The four changes above, all in `api/tools.py` / `api/main.py`.

## Alternatives considered

- **Per-integration tools** (an `adguard_stats` tool etc.) — precise,
  but every new integration would need code; generic entity lookup
  scales to any HA entity for free.
- **A larger model** — the residual errors are reading-comprehension
  noise of the 3B model; still the option to revisit (ADR-0018) if
  this keeps costing correctness.

## Consequences

- AdGuard *history* ("what happened yesterday") is still not covered:
  `sensor.*` domains are not indexed into `ha_history`, and only
  kWh sensors have a history tool. Live counters and switch states
  work; a generic "history/delta of any sensor" tool is the natural
  next step if that's wanted.
- Non-deterministic misreads remain (~1 in 3 on the door question was
  oddly phrased). Correct data reaches the model; the model
  occasionally mangles it.
