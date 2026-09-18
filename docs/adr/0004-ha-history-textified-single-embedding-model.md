# ADR-0004: Home Assistant history — textify events, keep one embedding model

- **Status**: accepted
- **Date**: 2026-09-18

## Context

Home Assistant history is mostly numeric/state data (sensor readings,
on/off, timestamps). Before deciding how to index it, we needed to answer:
does Velesha need a second embedding model or pipeline for "numeric" data,
separate from the text embedding model already used for Obsidian recipes
(ADR-0002)?

That depends entirely on what kind of question this needs to answer:

- Semantic, event-level questions ("коли я востаннє вмикав опалення?",
  "що відбувалось у вітальні ввечері?") — this is a text-retrieval problem.
- Numeric pattern questions ("знайди дні зі схожим патерном температури")
  — this is a time-series similarity problem, a genuinely different
  technique (statistical feature vectors or specialized time-series
  embeddings, not a sentence-embedding model like bge-m3).

Velesha's stated purpose (ADR-0001) is a personal assistant you ask
questions of in natural language — the semantic case is what's actually
needed right now.

## Decision

Textify HA events into short natural-language sentences at ingest time
(e.g. `"Опалення у вітальні увімкнено о 14:32, 18.09.2026"`), then embed
them with the same `bge-m3` model already used for every other source. No
second embedding model, no separate numeric pipeline.

Collection: `ha_history`, per ADR-0003 (one collection per source, same
vector size/distance as the rest since it's the same model).

## Alternatives considered

- **Separate embedding/representation for numeric time-series data** —
  correct approach if the goal were pattern/anomaly search over raw
  sensor curves, but that's a different feature, not what's being built
  now. Revisit with its own ADR if/when that need actually shows up —
  don't build it speculatively.

## Consequences

- One embedding model and one `llama-server` for the whole project,
  regardless of how many sources get added — simpler ops, consistent with
  ADR-0002.
- The HA ingest script's real complexity is the textification step (event
  → sentence), not the embedding step. That's where per-entity templates
  will need attention (a `light.living_room` toggle and a temperature
  sensor reading need different phrasing).
- If numeric pattern search is ever wanted, it's an additive feature (new
  collection, new pipeline) — this decision doesn't block it.
