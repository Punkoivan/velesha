# Jellyfin source

Indexes the Jellyfin library (movies + series, not individual episodes)
and per-user watch stats into Qdrant, as short Ukrainian sentences — see
ADR-0007 for why that granularity, why one user, and how titles get
cleaned up before embedding.

## Running it

Secrets are encrypted (ADR-0005) — never read from a plaintext file:

```bash
uv sync
sops --config /dev/null exec-env secrets.enc.env 'uv run ingest.py'
uv run search.py "фільми про мумію"
```

Requires the embedding server and Qdrant from the repo root README to be
up.
