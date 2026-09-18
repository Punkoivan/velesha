# Velesha — plan

A personal assistant with real memory of home and life data. Search first,
voice + avatar later.

## Phases

### Phase 1 — Memory (done)
Index life data sources into Qdrant, searchable semantically.

- [x] Obsidian recipes (`sources/obsidian/`) — see ADR-0002, ADR-0003
- [x] Home Assistant history (`sources/home_assistant/`) — via HA REST API
      (`/api/history/period`), long-lived access token (ADR-0005 for how
      it's stored); textified events per ADR-0004
- [x] Jellyfin library + watch stats (`sources/jellyfin/`) — via Jellyfin
      API, movie/series granularity per ADR-0007

### Phase 2 — Recall (done)
One search surface across all sources, not per-source scripts.

- [x] Unified query CLI (`cli/search.py`) across all collections
- [x] Basic Q&A (`cli/ask.py`): retrieves chunks, answers with a local
      Qwen2.5-3B-Instruct chat model (llama.cpp, port 8084) — see
      ADR-0010

### Phase 3 — Interface
- [x] OpenAI-compatible API (`api/`) — retrieval-grounded
      `/v1/chat/completions`, the integration point for HA's built-in
      "OpenAI Conversation" integration — see ADR-0011
- [ ] Wire it into Home Assistant (add the integration, point it at this
      host; close the streaming/system-prompt gaps noted in
      `api/README.md` if Assist needs them)
- [ ] CLI tool proper — command name TBD (not `sh`, that's taken; see notes)
- [ ] Voice input/output
- [ ] Avatar

### Phase 0 — Decouple from `abox` (done)
Was: Velesha leaned on the `abox` KinD cluster's Qdrant instance
(port-forwarded). Forced the issue when `abox` got rebuilt from a
different branch for a course exercise and took `obsidian_recipes` +
`ha_history` down with it. Now on the persistent `ha-addon-qdrant`
instance instead — see [ADR-0006](docs/adr/0006-qdrant-off-abox-onto-ha-addon.md).
Both sources re-indexed and verified working there.

## Notes

- Every non-obvious decision gets an ADR in `docs/adr/`, not just a mention
  in a commit message.
- Data sources are UA-heavy (recipes, notes) — embedding model choice
  already accounts for this (bge-m3, see ADR-0002).
