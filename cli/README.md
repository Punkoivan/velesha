# CLI

One search surface across every indexed source (Phase 2 of `PLAN.md`),
instead of per-source `sources/*/search.py` scripts. Queries every known
Qdrant collection with the same embedding and merges results by score.

## Running it

```bash
uv sync
sops --config /dev/null exec-env ../secrets.enc.env 'uv run search.py "коли вмикали телевізор"'
sops --config /dev/null exec-env ../secrets.enc.env 'uv run search.py --source jellyfin_library "мумія"'
```

Requires the embedding server and Qdrant from the repo root README to be
up.
