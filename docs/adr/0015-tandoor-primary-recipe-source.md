# ADR-0015: Tandoor becomes the primary (writable) recipe source; Obsidian recipes migrated

- **Status**: accepted
- **Date**: 2026-09-19

## Context

ADR-0014 added Tandoor as a second, separate recipe collection alongside
`obsidian_recipes`, deliberately not merged. Two things changed that
call for revisiting that split:

1. Tandoor's REST API turned out to support full CRUD (`POST
   /api/recipe/` confirmed, with nested writes for `steps` and
   `keywords`), which the earlier ADR hadn't checked — it only looked at
   Tandoor as a read source.
2. The user wants kitchen-equipment-aware recipe suggestions, and some
   Obsidian recipe notes already tag required equipment informally (a
   `техніка:` frontmatter field, e.g. `техніка: духовка`) — a signal
   worth keeping queryable, not scattered across two collections with
   inconsistent tagging conventions.

Having two writable-in-principle recipe stores (a markdown vault and a
recipe manager with its own structured schema) is redundant, and only
one of them (Tandoor) has an API a program can write to reliably — the
Obsidian route Velesha used to write to was "generate a markdown file,"
which doesn't get any of Tandoor's structure (ingredients as data,
servings, timing, keywords) for free.

## Decision

Tandoor is now the primary recipe source. `CLAUDE.md`'s standing
instruction to write recipes into the Obsidian vault is changed: notes
still go to Obsidian, **recipes go to Tandoor via its API**.

All 34 existing Obsidian recipe notes were migrated
(`sources/tandoor/migrate_from_obsidian.py`, one-time script): one
Tandoor recipe per note, frontmatter `tags` and any `техніка` field
folded into Tandoor keywords, the note's full body kept verbatim as a
single step's instruction text — **not** parsed into Tandoor's
structured per-ingredient model. The notes' formatting is inconsistent
across files (compare the well-structured `Індича гомілка...` against
the free-form `Соус томатний`, no headings at all) — a regex/heuristic
ingredient parser would be fragile and risks silently mangling
quantities or units in a domain (cooking) where that's a real-world
correctness problem, not just cosmetic. Preserving the original text
losslessly was judged safer than a lossy structured parse.

The original `.md` files are left untouched in the Obsidian vault (not
deleted, not archived elsewhere) — explicit user choice, "leave as is."
`obsidian_recipes` is removed from `cli/search.py`'s and `api/main.py`'s
`COLLECTIONS` lists to stop returning now-duplicate results; the
collection itself is **not** deleted from Qdrant (cheap to leave, no
reason to destroy data), just no longer queried.

`sources/tandoor/textify.py` now includes keywords in the embedded text
(`Теги: ...`), so equipment tags like "Ninja Combi" or "духовка" are
part of what semantic search matches against — verified: searching
"щось на Ninja" surfaces the Ninja-tagged recipes correctly.

## Alternatives considered

- **Parse ingredients into Tandoor's structured model during migration**
  — more "correct" Tandoor usage (proper shopping-list support, unit
  conversions), but the source data's inconsistency makes this
  meaningfully riskier than it's worth for a one-time migration of 34
  recipes; can be done by hand later, recipe by recipe, if the structured
  fields turn out to matter.
- **Keep both collections active, dedupe at query time** — more moving
  parts (dedup logic, deciding which copy wins when both match) for a
  problem `COLLECTIONS` list editing already solves for free.
- **Delete the Obsidian recipe notes now that they're migrated** — user
  explicitly declined; kept as-is.

## Consequences

- Future recipe write-back (the agentic idea already noted in
  `PLAN.md`'s "Future ideas") targets Tandoor's API, not the Obsidian
  vault — this ADR is what that future work should build on.
- `sources/obsidian/` (`index.py`/`search.py`) still exists and still
  works against the vault's `рецепти/` folder, but is no longer part of
  the active `COLLECTIONS` lists — effectively dormant unless someone
  explicitly re-adds it. Not deleted; ADR-0014's original "kept
  separate" reasoning still holds if a future need to search old,
  frozen Obsidian content specifically ever comes up.
- `CLAUDE.md` (outside this repo, at `/home/punka/CLAUDE.md`) was edited
  directly — global instruction, not just a Velesha-local decision, so
  it now applies to any future session working with this user's recipes.
