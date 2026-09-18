# ADR-0001: Project name and mission

- **Status**: accepted
- **Date**: 2026-09-18

## Context

This started as a small script to semantically search Obsidian recipes, then grew
into a broader idea: a personal assistant that indexes data about your life
(Home Assistant history, Jellyfin media library, Obsidian notes/recipes, more
later) and can answer questions about it — eventually with a voice interface
and an avatar.

That scope is different enough from "a search script" that it deserves its
own repo, its own name, and decisions written down as they're made, instead
of living as a subfolder of the `harness-course` learning repo.

## Decision

Name: **Velesha** (Велеша) — from Veles, the Slavic god of wisdom, magic, and
knowledge, with a `-sha`/`-ша` diminutive suffix (same pattern as Sasha,
Misha) to make it read as a character/companion name rather than a database
or library. The "ha" also nods to Home Assistant, one of the first and
primary data sources.

Checked before committing to it:
- `veles` alone is taken on PyPI and npm, and collides conceptually with
  `VelesDB` (an existing AI-agent memory engine, 90+ stars) — confusing to
  reuse.
- `velesha` is free on GitHub, PyPI, and npm, with no adjacent projects.

Mission: a personal assistant with real memory of your home and life data,
queryable via search now, and via voice/avatar later.

## Alternatives considered

- `veles-ha` — clearer but reads as a technical HA integration, not a
  product/character.
- `second-memory`, `homelab-rag`, `domovyk`, `pantry` — considered during
  naming brainstorm; dropped in favor of a name with a mythological anchor
  and room for a visual identity (avatar).

## Consequences

- The recipe-indexing code built during the course (see `harness-course`
  repo) moves here as the seed of `sources/obsidian`.
- Future ADRs should record source integrations, storage choices, and the
  eventual voice/avatar architecture as they're decided, not after the fact.
