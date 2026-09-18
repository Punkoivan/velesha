# ADR-0008: Structured Jellyfin queries go straight to the API, not through an MCP server

- **Status**: accepted
- **Date**: 2026-09-19

## Context

"What's the last movie I watched?" needs an exact sort by
`UserData.LastPlayedDate` — a structured query. The `jellyfin_library`
Qdrant collection (ADR-0007) doesn't carry that as a filterable/sortable
payload field, only baked into the embedded sentence text, so semantic
search over it can't answer this reliably; it was answered instead with
a one-off script calling `jellyfin_client.get_items()` directly and
sorting client-side.

That raised the question of whether to formalize "live structured Jellyfin
queries" as an MCP server, so any MCP-capable client (this session
included) could call it as a tool directly.

## Decision

No MCP server for now. Structured queries stay as small scripts in
`sources/jellyfin/`, built directly on `jellyfin_client.py` — the same
REST client `ingest.py` already uses, same secrets (ADR-0005). First one:
`recent.py`, listing the most recently watched items.

## Alternatives considered

- **A Jellyfin MCP server** (community one, or a Velesha-owned one) —
  would give any MCP client ad-hoc tool access to live Jellyfin state
  without writing a new script per question. Rejected for now: it's a
  second, always-running process with its own auth surface, duplicating
  `jellyfin_client.py` behind a protocol boundary, for a need
  (occasional structured lookups) that a handful of scripts already
  cover. Revisit if structured-query needs grow enough (many different
  query shapes, or a need for other MCP clients besides this one) to
  outweigh running a persistent server.
- **Add `last_played_date` etc. as structured Qdrant payload fields** —
  would let Qdrant's own filter/sort handle this without touching the
  live API. Still on the table, orthogonal to the MCP question; not done
  here because the live API already answers it correctly and Jellyfin
  data changes are frequent enough (every playback) that Qdrant would
  need re-ingestion to stay current anyway.

## Consequences

- `sources/jellyfin/` ends up with two kinds of scripts: `ingest.py` +
  `search.py` for the semantic path (Qdrant, requires re-indexing to
  stay fresh), and small direct-API scripts like `recent.py` for
  structured/live questions (always current, no embedding involved).
- No new always-on process, no new auth surface beyond the existing
  `secrets.enc.env`.
- If a future phase wants "any MCP client can ask Velesha structured
  questions," this ADR is the one to revisit and supersede.
