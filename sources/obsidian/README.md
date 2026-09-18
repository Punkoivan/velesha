# Obsidian source

Indexes notes from the Obsidian vault into Qdrant. Currently: recipes
(`Домашнє/рецепти/`) → collection `obsidian_recipes`.

```bash
uv sync
uv run index.py
uv run search.py "щось із куркою на вечерю"
```

Requires the embedding server and Qdrant from the repo root README to be up.
