# Tandoor source

Indexes recipes from [Tandoor Recipes](https://docs.tandoor.dev/) (the
user's self-hosted instance, running as an add-on alongside Home
Assistant) into Qdrant — title, description, and every step's
instructions + ingredients, textified into one blob per recipe. See
ADR-0014 for why this is a separate collection from
`sources/obsidian/`'s recipes rather than merged into it.

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
