# Obsidian source

**Dormant as of ADR-0015**: recipes moved to `sources/tandoor/` as the
primary, writable recipe source. This source's `obsidian_recipes`
collection still exists in Qdrant (not deleted) but is excluded from
`cli/search.py`'s and `api/main.py`'s active `COLLECTIONS` lists, so its
content no longer shows up in search/Q&A results.

Kept around in case old, frozen Obsidian recipe content is ever wanted
specifically — not deleted per ADR-0014's original reasoning.

```bash
uv sync
uv run index.py
uv run search.py "щось із куркою на вечерю"
```

Requires the embedding server and Qdrant from the repo root README to be up.
