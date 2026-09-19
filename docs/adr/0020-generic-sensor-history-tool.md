# ADR-0020: One generic `get_sensor_history` tool replaces `get_energy_usage`

- **Status**: accepted
- **Date**: 2026-09-19

## Context

ADR-0018's `get_energy_usage` only covered kWh counters; ADR-0019 showed
live values work for any entity but "what happened yesterday" had no
tool (sensors aren't indexed into `ha_history`). Real-data checks before
coding changed the design:

- AdGuard counters have **no `state_class`** and are not strictly
  monotonic: `dns_queries` dips slightly every hour as old queries leave
  AdGuard's stats window (16 small decreases over 4 days, e.g.
  3292 → 3291, 4176 → 3995). Treating a drop as a "reset" and summing
  increments (right for `total_increasing` counters) would overcount, so
  they get net change first → last instead.
- The washing machine's sensor was renamed
  (`sensor.pralka_summation_delivered`, `total_increasing`); resolution
  by friendly name kept working, an id-based tool would not have.

## Decision

`get_sensor_history(entity_name, start_date, end_date?)` in
`api/tools.py`, arithmetic done in code because the 3B model can't
reliably subtract or compare timestamps. Summary by entity kind:

- **on/off entities** (switch, binary_sensor, ...): activation count,
  total time on, last time on.
- **`total_increasing` / kWh counters**: sum of increments, a drop
  treated as a reset (verified: 0.55 kWh for the washing machine on
  2026-09-18, same as the hand-computed value).
- **other numeric sensors**: change over the period, min/max/mean. For
  count-like units (`queries`, `requests`) a single plain sentence with
  only the change — with min/max/mean the model read the start value as
  the answer to "how many".

Dates accept `YYYY-MM-DD`, `сьогодні`, `вчора` (resolved in code, not by
the model). Timezone is `Europe/Kyiv` via `zoneinfo` (fixed +3 fallback
if tzdata is missing), so day boundaries follow DST. If the hint mentions
electricity (`кВт`, `енерг`, `спожив`, `електр`) resolution is limited to
`kWh` sensors, since a bare device name resolves to its switch by design
(ADR-0019). Data missing for the period → says so, noting HA keeps only
~10 days of detailed history.

`get_energy_usage` is removed (its behavior is a case of this tool).

## Alternatives considered

- **Keep per-type tools** — more tool choices for a 3B model to pick
  between, which is the weakest part of the system.
- **Give the model raw points** — it cannot do the arithmetic reliably.
- **HA long-term statistics (websocket)** for periods > ~10 days —
  useful, but a separate integration; not needed for the questions asked.

## Consequences

- Verified through the agent: washing machine 0.55 kWh; total AdGuard
  queries yesterday 2722 (stable, 2/2); TV activations, bedroom
  temperature min/max/mean.
- **Still model-noise limited**: "скільки запитів *заблокував* AdGuard
  вчора?" sometimes goes to `get_live_state` (and answers with a live
  counter) instead of history, and two prompt-wording bugs were found
  the hard way — a concrete example in a tool description ("AdGuard
  Захист") got copied by the model into unrelated calls, so descriptions
  now use generic wording plus explicit disambiguation ("всього" vs
  "заблоковано"). A stronger model is the real fix if this matters.
- Detailed history depth is bounded by HA's recorder (~10 days default).
