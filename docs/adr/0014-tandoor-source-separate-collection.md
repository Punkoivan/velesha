# ADR-0014: Tandoor recipes get their own collection, not merged with Obsidian's

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The user has a second recipe source: [Tandoor
Recipes](https://docs.tandoor.dev/), self-hosted as an add-on alongside
Home Assistant (`http://ha.punka.space:9928`), with its own structured
data model (steps, per-step ingredients with amount/unit/food, servings,
timing) — a REST API with token auth, not markdown files like
`sources/obsidian/`'s recipes vault.

This raised the same question ADR-0007 asked for Jellyfin: does a new
kind of source need its own collection, or should it fold into an
existing one?

## Decision

New collection: `tandoor_recipes`, separate from `obsidian_recipes`.
Textification (`sources/tandoor/textify.py`) builds a single text blob
per recipe: title, description, servings/timing, then each step's name,
its ingredients formatted as `amount unit food (note)`, and its
instruction text — same embedding model (`bge-m3`), same vector
size/distance as every other collection (ADR-0003).

Kept separate rather than merged into `obsidian_recipes` because:
- The two sources are independently maintained (a Tandoor recipe being
  added/edited doesn't touch the Obsidian vault and vice versa) —
  merging would need a shared payload shape and a merged re-ingest
  story neither source actually needs on its own.
- No stated need yet to treat "recipe" as one unified concept across
  sources; `cli/search.py` and `api/`'s retrieval already search across
  all collections together, which gets the practical benefit (one
  question like "щось із куркою" surfaces results from both) without
  forcing a shared collection.

## Alternatives considered

- **One `recipes` collection across both sources** — would need a
  `source` payload field to disambiguate and a decision about what
  happens if the same recipe exists in both (not currently the case, 5
  Tandoor recipes vs. the Obsidian vault's own set, no observed overlap)
  — deferred; revisit if duplicate recipes across sources actually
  becomes a problem worth solving.
- **Only index Tandoor, drop the Obsidian recipes vault** — not
  considered seriously; the Obsidian vault is the user's primary note
  system (per `CLAUDE.md`), Tandoor is an addition, not a replacement.

## Consequences

- `cli/search.py`'s and `api/main.py`'s `COLLECTIONS` list now has four
  entries; both already iterate the list generically, so no other code
  changed.
- Small dataset today (5 recipes) — `ingest.py` fetches every recipe's
  full detail on each run (no incremental/delta sync), fine at this
  size; revisit if the Tandoor instance grows enough that a full
  re-fetch becomes slow.
