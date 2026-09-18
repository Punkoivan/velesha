# ADR-0003: Qdrant as the vector store, one collection per source

- **Status**: accepted; connection target superseded by
  [ADR-0006](0006-qdrant-off-abox-onto-ha-addon.md) — "one collection per
  source" still stands, only *which* Qdrant instance changed
- **Date**: 2026-09-18

## Context

Need somewhere to put the vectors from ADR-0002, queryable by similarity,
filterable by payload (e.g. "only recipes", "only HA events from last
month"), and durable across restarts.

## Decision

Qdrant, addressed at `localhost:6333`. For now this points at the Qdrant
instance already running inside the `abox` KinD cluster (see
`harness-course`), reached via `kubectl port-forward`. One collection per
source (`obsidian_recipes`, later `ha_history`, `jellyfin_library`, ...)
rather than one shared collection with a `source` filter field — keeps
schemas independent as sources are added and makes it trivial to re-index
or drop a single source.

Point IDs are a stable hash of the source's natural identifier (file path
for Obsidian notes) so re-running an ingest updates existing points instead
of duplicating them.

## Alternatives considered

- **A dedicated Velesha Qdrant instance** (own docker-compose, outside
  `abox`) — cleaner separation from the course cluster, but the course
  cluster is already up and this is meant to be validated end-to-end first;
  revisit once `abox` stops being a dependency Velesha shouldn't have.
- **One shared collection with a `source` payload field** ß simpler to
  query "across everything," but couples every source to the same vector
  size and payload shape. Rejected for now; can still build a
  cross-collection search later without this.

## Consequences

- Velesha currently has a soft runtime dependency on the `abox` cluster   being up and port-forwarded. This is a known temporary coupling, not a  design goal - worth a follow-up ADR when it's time to decouple.
- Adding a new source means: new collection, new ingest script, same
  embedding pipeline.
