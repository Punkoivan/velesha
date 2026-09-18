# ADR-0016: Retrieve top-N per collection, not top-N globally; bump chat context to 8192

- **Status**: accepted
- **Date**: 2026-09-19

## Context

User report: "що у нас є з випічки" (what do we have for baking) returned
only 2 recipes, though 14 Tandoor recipes carry the `випічка` keyword.
Root cause, confirmed by inspecting `retrieve_context`'s actual output:
`api/main.py` and `cli/ask.py` both queried each collection for
`CHUNKS_PER_QUERY` (5) results, merged everything, sorted by raw cosine
score, and kept only the **global** top 5. For this query, 2 of those 5
slots went to `ha_history` events ("духовка: увімкнено о 18:32...") —
the word "духовка" (oven) scored high against a baking question despite
being a completely unrelated kind of content (a device-toggle log, not a
recipe) — crowding out real recipes that would otherwise have made the
cut.

This is a structural problem, not a tuning-the-number problem: raw
cosine similarity scores from `bge-m3` aren't calibrated to be
comparable across semantically different content domains (recipe text
vs. terse HA event sentences vs. Jellyfin watch-stat sentences). A
global top-N will always be vulnerable to one collection's scores
running systematically higher for a given query and starving the
others, regardless of what N is chosen.

## Decision

Take the top `CHUNKS_PER_COLLECTION` (4) results from **each** active
collection independently and include all of them in context — no
merge-then-global-truncate step. A recipe question now always gets up to
4 recipe chunks, an HA question up to 4 HA chunks, etc., regardless of
how the raw scores compare across collections.

With 3 active collections × 4 chunks × 600 chars (`MAX_CHUNK_CHARS`,
ADR-0010), worst case is bigger than before — this overflowed the chat
model's 4096-token context (`Context size has been exceeded` from
`llama-server`, confirmed in testing). Fixed by raising the chat
server's context to `-c 8192` (matches the embedding server's context
size already, ADR-0002) — verified working end-to-end afterward: 71
completion tokens, 1761 prompt tokens, well under 8192.

Applied identically to `api/main.py`'s `retrieve_context` and
`cli/ask.py`'s use of `search_all` (which already queried per-collection
internally; the bug was the trailing `[: args.chunks]` global slice on
top of that — removed).

## Alternatives considered

- **Just raise the global N** (e.g. 5 → 15) — doesn't fix the underlying
  issue, only makes it statistically less likely to bite for any single
  query; a collection with a systematically higher score distribution
  for some query shape could still starve others even at N=15.
- **Score normalization per collection** (e.g. z-score or min-max per
  collection before merging) — would fix the comparability problem more
  rigorously, but adds real complexity (needs per-collection score
  statistics, tuning) for a fix that per-collection capping already
  delivers with a two-line change.
- **Detect query intent and route to one collection** (e.g. "recipe
  question" → only query `tandoor_recipes`) — more precise, but needs
  either a classifier call (extra LLM round-trip, extra latency) or
  fragile keyword heuristics; per-collection capping gets most of the
  benefit for free.

## Consequences

- Still not exhaustive: 14 recipes carry `випічка`, only up to 4 Tandoor
  chunks make it into context, so "what do we have for baking" lists a
  sample, not the complete set — already noted as a known gap in
  `PLAN.md`'s "Exact equipment filtering" future idea, unchanged by this
  fix and not this ADR's problem to solve.
- Context usage roughly doubles in the worst case (up to `3 ×
  CHUNKS_PER_COLLECTION` chunks instead of a flat 5) — the `-c 8192`
  bump accounts for that; RAM headroom checked before and after
  (available memory was actually slightly higher post-restart, ~6.3GB,
  well within the ADR-0012 "always-on" posture).
- If a fourth collection is added later, worst-case context grows
  further — revisit `CHUNKS_PER_COLLECTION` and/or `-c` again then,
  rather than assuming today's numbers hold indefinitely.
