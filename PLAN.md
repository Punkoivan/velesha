# Velesha — plan

A personal assistant with real memory of home and life data. Search first,
voice + avatar later.

## Phases

### Phase 1 — Memory (in progress)
Index life data sources into Qdrant, searchable semantically.

- [x] Obsidian recipes (`sources/obsidian/`) — see ADR-0002, ADR-0003
- [ ] Home Assistant history (`sources/home_assistant/`) — via HA REST API
      (`/api/history/period`), long-lived access token
- [ ] Jellyfin library + watch stats (`sources/jellyfin/`) — via Jellyfin API

### Phase 2 — Recall
One search surface across all sources, not per-source scripts.

- [ ] Unified query CLI (`velesha search <query>`) across all collections
- [ ] Basic Q&A: retrieve relevant chunks, answer with an LLM (local via
      llama.cpp, chat model this time — see future ADR)

### Phase 3 — Interface
- [ ] CLI tool proper — command name TBD (not `sh`, that's taken; see notes)
- [ ] Voice input/output
- [ ] Avatar

### Phase 0 — Decouple from `abox` (housekeeping, not urgent)
Velesha currently leans on the `abox` KinD cluster's Qdrant instance
(port-forwarded). Fine for now; revisit once Phase 1 is solid — either its
own lightweight Qdrant (docker-compose) or a decision to keep sharing
`abox`.

## Notes

- Every non-obvious decision gets an ADR in `docs/adr/`, not just a mention
  in a commit message.
- Data sources are UA-heavy (recipes, notes) — embedding model choice
  already accounts for this (bge-m3, see ADR-0002).
