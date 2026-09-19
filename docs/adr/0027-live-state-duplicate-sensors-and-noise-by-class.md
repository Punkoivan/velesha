# ADR-0027: `get_live_state` — freshest sensor first, noise filtered by device class

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The user said the front door was open while the agent had answered
"зачинені". Cause, read from HA: two sensors describe one door —
`вхідні двері` (**on**, updated 15:21:55) and `вхідні двері Відкриття`
(**off**, last updated 11:23 and never again). `get_live_state` returned
both as equally relevant near-tie candidates, so a model read either at
random; a battery sensor also leaked in, because HA had renamed
`sensor.*_batareia` to `*_batareya` and the entity-id-substring noise
filter silently stopped matching. An earlier evaluation of the door
question was therefore unreliable for both models (ADR-0026 correction).

The stale duplicate is part of the HA-side mess the user chose to clean
up later; the agent has to be safe until then.

## Decision

- **Noise is judged by attributes, not id spelling**: entities with
  `device_class` in `battery/identify/firmware/update/restart`, or domain
  `update/button/select`, are never matched (the old id substrings stay
  as an extra). Immune to renames.
- **Near-tie candidates are ordered by `last_changed`, newest first**, each
  line shows when it last changed ("змінилось 15:21"), and when more than
  one line is returned the tool adds "якщо показання різняться — вірне те,
  що змінилось пізніше (перший рядок)". Output for the door now:
  `вхідні двері: відчинено (змінилось 15:21)` /
  `вхідні двері Відкриття: закрито (змінилось 11:23)`.

Verified: door question 3/3 on `gpt-4.1-mini` and 2/2 on the local
model, both answering "відчинені", with the correct change time.

## Alternatives considered

- **Return only the freshest sensor** — hides the conflict from the
  model and the user; showing both with timestamps lets the answer say
  what is known. It also fails when the *stale* one is the only match.
- **Drop the "Відкриття" duplicate by name** — brittle, and the real fix
  (delete/repair the duplicate in HA) belongs to the user.
- **Wait for the user to clean HA** — the agent would keep giving wrong
  answers about a security-relevant state in the meantime.

## Consequences

- If the duplicate is the *only* fresh reading of some device, ordering
  by recency still trusts the newest — right for a stuck sensor, wrong
  for a sensor that reports spuriously; not observed.
- Times are shown in Kyiv time; "змінилось" is when the *state* changed,
  not when it was last polled.
