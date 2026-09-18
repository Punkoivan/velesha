# Tandoor source

**Primary recipe source** (ADR-0015) — indexes recipes from [Tandoor
Recipes](https://docs.tandoor.dev/) (the user's self-hosted instance,
running as an add-on alongside Home Assistant) into Qdrant: title,
description, keywords, and every step's instructions + ingredients,
textified into one blob per recipe.

All Obsidian recipe notes were migrated here via
`migrate_from_obsidian.py` (one-time script, see ADR-0015) — new/updated
recipes go through Tandoor's own UI or API now, not as Obsidian notes.

## Running it

Secrets are encrypted (ADR-0005) — never read from a plaintext file:

```bash
uv sync
sops --config /dev/null exec-env ../../secrets.enc.env \
  'sops exec-env secrets.enc.env "uv run ingest.py"'
sops exec-env ../../secrets.enc.env 'uv run search.py "паста з кунжуту"'
```

Requires the embedding server and Qdrant from the repo root README to be
up.
