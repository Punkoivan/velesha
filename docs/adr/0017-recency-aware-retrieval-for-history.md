# ADR-0017: Recency-aware retrieval for "коли востаннє" questions

- **Status**: accepted
- **Date**: 2026-09-19

## Context

User report: "коли востаннє було вімкнено телевізор?" (when was the TV
last turned on) returned a wrong answer — "18:54, 17.09.2026" the first
time, "11:37, 18.09.2026" (an *off* event) the second. Ground truth,
confirmed directly against the HA history API: the real last-on event
was `18:32:33 UTC, 18.09.2026`.

Two distinct bugs stacked here, found by debugging step by step:

1. **The right chunk was sometimes retrieved but not recognized as most
   recent.** `switch.tv`'s history is 13 points in
   `ha_history`, all near-identical text ("телевізор: [у]вимкнено о
   HH:MM, DD.MM.YYYY") — cosine similarity between them is barely
   distinguishable, so the top-`CHUNKS_PER_COLLECTION` (4, per ADR-0016)
   by raw vector score is close to arbitrary among these 13 candidates.
   Even when the correct 18:32 event *was* in that top-4, a small local
   model (Qwen2.5-3B) asked to eyeball a semantically-ordered (not
   chronological) list and pick the most recent one got it wrong —
   confirmed by reproducing the exact same wrong answer HA got, with the
   correct event present but not first.
2. **The right chunk sometimes wasn't retrieved at all.** A second test
   run, same question, returned a *different* top-4 (missing the
   correct 18:32 event entirely, replaced by a duplicate "вимкнено о
   11:37" entry appearing twice) — the top-4-by-score set isn't stable
   or complete enough across 13 near-duplicate candidates to reliably
   contain the true most-recent one.

## Decision

Two changes, together closing both gaps:

**1. Sort by real timestamp, not semantic score, wherever one exists.**
`ha_history` payloads carry `last_changed` (an ISO timestamp string,
set at ingest — `sources/home_assistant/ingest.py`). Both `api/main.py`
and `cli/ask.py` now sort retrieved chunks by `last_changed` descending
before building context, falling back to original score order (stable
sort, missing key treated as empty string) for collections without a
timestamp field — no per-collection branching needed, the sort key's
absence is itself the fallback.

**2. Cast a wider semantic net before picking, to fix recall.** Query
each collection for `CANDIDATE_POOL` (20) candidates instead of just
`CHUNKS_PER_COLLECTION` (4), *then* sort by recency and keep the top 4.
This fixes bug 2: near-duplicate-text entities like a single switch's
on/off history are usually all within the top 20 nearest neighbors even
if their *relative* ranking among each other is close to noise — once
they're all candidates, exact timestamp sort picks the actual most
recent one reliably. Verified: 3/3 repeated identical queries after the
fix returned the correct 18:32 answer (previously inconsistent across
runs).

Applied in `api/main.py`'s `retrieve_context` (per-collection,
query→sort→slice) and `cli/ask.py` (new `pick_chunks` helper, since
`cli/search.py`'s `search_all` is shared with `search.py`'s own
relevance-ranked display, which must stay score-ordered and was not
changed).

## Alternatives considered

- **Exact structured query instead of RAG for "коли востаннє" questions**
  (detect intent, query HA/Qdrant directly by entity + `ORDER BY
  last_changed`, skip the LLM's judgment entirely) — the most correct
  fix and the same shape as ADR-0008's Jellyfin `recent.py`, but needs
  intent detection (which questions are "recency" questions?) that
  doesn't exist yet in the conversational path (`api/`); the
  sort-plus-wider-pool fix gets most of the benefit today without that
  new component. Revisit if this class of question keeps causing
  trouble after this fix.
- **Just raise `CHUNKS_PER_COLLECTION`** (e.g. 4 → 20, skip the
  candidate-pool-then-slice split) — simpler, but sends far more context
  to the chat model on every question, including ones where recency
  doesn't matter (undoes ADR-0016's context-budget reasoning for no
  benefit on non-recency questions).

## Consequences

- Every collection now costs one wider Qdrant query (`CANDIDATE_POOL`
  results fetched, `CHUNKS_PER_COLLECTION` used) instead of a narrow
  one — more Qdrant-side work per question, negligible at this
  collection size (hundreds of points, not millions).
- Still LLM-mediated, not a guaranteed-correct structured answer: if a
  genuinely ambiguous or very sparse history makes even a 20-candidate
  pool miss the right event, the model can still get it wrong. The
  "exact structured query" alternative above is the real fix if that
  turns out to matter in practice.
- `MAX_CHUNK_CHARS` unchanged (600) and chat context stays at 8192
  (ADR-0016) — the wider pool is filtered back down to the same final
  chunk count before hitting the model, so context budget math from
  ADR-0016 still holds.
