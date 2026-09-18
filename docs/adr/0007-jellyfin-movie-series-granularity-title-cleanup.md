# ADR-0007: Jellyfin source — movie/series granularity, per-user watch stats, light title cleanup

- **Status**: accepted
- **Date**: 2026-09-18

## Context

Jellyfin's item hierarchy is Movie / Series → Season → Episode (211
episodes across only 10 series in this library). Watch state
(`UserData`: `Played`, `PlayCount`, `LastPlayedDate`, `PlayedPercentage`)
is **per-user**, not global — Jellyfin has multiple accounts on this
server (`home`, `qq`, `tv`, `Yuliia`).

Two questions, same shape as ADR-0004 for HA history:

1. What granularity actually answers the kind of question Velesha needs
   to answer ("чи дивився я Мумію", "що я дивився в квітні"), without
   drowning the index in noise?
2. Real filenames in this library are release-group dumps, e.g.
   `"01. The Mummy"`, `"Batman  The Animated Series (1992-1999) [Ukr Eng
   Fra, Sub Eng Fra Spa] DVD9 [Hurtom]"` — not clean titles.

## Decision

**Granularity**: index `Movie` and `Series` items only, not individual
`Episode`s. A series' `UserData.PlayedPercentage` already aggregates
watch progress across all its episodes, so episode-level rows would add
211 near-duplicate points for zero extra query value at the level Velesha
operates ("did I watch X", not "did I watch S02E07 of X").

**Per-user scope**: `JELLYFIN_USER_ID` is a required secret (ADR-0005),
resolved once via `/Users` and hardcoded per-source, not looked up by
name at runtime. Watch stats are scoped to one account (`tv`) rather than
merged across the household — merging would need a policy decision
(whose "watched" counts?) that isn't needed yet with one account in
active use.

**Title cleanup**: strip square-bracket release tags (`\[.*?\]` — codec/
sub-language/group info, always bracketed in this library) and leading
numeric prefixes (`"01. "`) before building the sentence. Left everything
else (parenthetical years/ranges, encoding names outside brackets)
untouched — a regex heuristic good enough for this library's naming
conventions, not a general-purpose title parser. `Genres`/`Overview` are
fetched but frequently empty here, so textification leans on title + year
+ watch stats, same as HA leaning on entity state (ADR-0004) rather than
richer metadata that isn't reliably present.

## Alternatives considered

- **Index episodes too, for "did I watch episode N" queries** — real
  question, but not one asked yet; adds 20x the points for a library this
  size. Revisit with its own ADR if per-episode queries actually come up.
- **A real title-parsing library** (guessit, etc.) — more correct, but
  another dependency for a cleanup step that's cosmetic (search still
  works on the raw title as a fallback via the embedding); regex heuristic
  is the smaller move for now.
- **Aggregate watch stats across all Jellyfin users** — deferred; no
  stated need yet, and merging semantics (union vs. per-user rows) is a
  decision to make when it's actually needed, not speculatively.

## Consequences

- Collection: `jellyfin_library`, same vector size/distance as every
  other source (ADR-0003), single embedding model (ADR-0002).
- Adding a second Jellyfin account later is additive (new
  `JELLYFIN_USER_ID`, new ingest run) but current payloads don't carry a
  user dimension — revisit if/when that's wanted.
- Messy titles that don't match the bracket/numbering heuristic pass
  through unchanged into the sentence — acceptable since the embedding
  still captures meaning from the noisy string, just less cleanly.
