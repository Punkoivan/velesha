# Velesha — plan

A personal assistant with real memory of home and life data. Search first,
voice + avatar later.

## Phases

### Phase 1 — Memory (done)
Index life data sources into Qdrant, searchable semantically.

- [x] Obsidian recipes (`sources/obsidian/`) — see ADR-0002, ADR-0003.
      **Dormant since ADR-0015**: migrated to Tandoor, collection no
      longer in the active search set.
- [x] Home Assistant history (`sources/home_assistant/`) — via HA REST API
      (`/api/history/period`), long-lived access token (ADR-0005 for how
      it's stored); textified events per ADR-0004
- [x] Jellyfin library + watch stats (`sources/jellyfin/`) — via Jellyfin
      API, movie/series granularity per ADR-0007
- [x] Tandoor recipes (`sources/tandoor/`) — self-hosted alongside HA, via
      its REST API. Now the **primary recipe source**: all 34 Obsidian
      recipe notes migrated in, `CLAUDE.md` updated to write new recipes
      here instead of Obsidian — see ADR-0014, ADR-0015

### Phase 2 — Recall (done)
One search surface across all sources, not per-source scripts.

- [x] Unified query CLI (`cli/search.py`) across all collections
- [x] Basic Q&A (`cli/ask.py`): retrieves chunks, answers with a local
      Qwen2.5-3B-Instruct chat model (llama.cpp, port 8084) — see
      ADR-0010

### Phase 3 — Interface (integration done, rest not started)
- [x] OpenAI-compatible API (`api/`) — see ADR-0011
- [x] Wired into Home Assistant as `conversation.velesha`, via HA's
      built-in `llama_cpp` integration (not "OpenAI Conversation" — that
      dropped custom-`base_url` support; see ADR-0013). Streaming
      implemented (HA requires it).
- [x] **Tool-calling agent, not blind RAG** (ADR-0018): `api/` now gives
      the model three read-only tools — `search_knowledge` (the old
      always-on retrieval, now on-demand), `get_live_state` (live HA
      device/sensor state — the door-sensor "RAG only sees a snapshot"
      gap), `get_sensor_history` (period summaries computed in code — kWh used,
      counter change, on/off counts; ADR-0020, not a retrievable fact at all). Verified end-to-end through HA on three real bugs
      this fixed. Device control still out of scope (HA's Assist tool
      schema received, not acted on).
- [x] **First state-changing tool** (ADR-0021): `play_on_jellyfin_device`
      starts a movie/series (next unwatched episode) on Kodi via Jellyfin's
      session API, waits for Kodi to boot, Kodi-only allowlist, offered to
      the model only when the user's message has an explicit command verb.
- [x] **Jellyfin playback control** (ADR-0022): pause/resume/stop/next/
      previous on Kodi. Hosted-model switch (`CHAT_API_KEY`) coded but
      untested until a key is supplied.
- [x] **qBittorrent** (ADR-0023): status (session vs all-time upload, ratio-0
      counts), filtered lists, add-with-category (code-gated; category must
      come from the user's words and have a save path).
- [x] **Toloka.to search + add** (ADR-0024): search by title with the user's
      login (title/size/seeders), pick by number and name the section,
      `.torrent` uploaded to qBittorrent; disk-space guard.
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

### Future ideas (not scheduled)
- **Swap chat provider to Claude API** — user has separate Anthropic API
  credits (confirmed real, not claude.ai Pro/Max subscription usage,
  which is a different pool and can't power a third-party app). Would
  mean a `CHAT_PROVIDER=local|anthropic` switch in `api/` and
  `cli/ask.py`, trading RAM/local-only for quality and cost-per-token.
  Deliberately deferred — staying fully local for now.
- **Agentic write-back** — e.g. "find a borscht recipe in my notes; if
  it's not there, search the web, pick the best one, add it to the
  base." Needs two things Velesha doesn't have yet: a web search tool
  and a write tool (Tandoor's API supports it, ADR-0015, just not
  exposed to the agent yet). ADR-0018 showed Qwen2.5-3B is a more
  capable tool-caller than originally assumed here (correct tool +
  arguments across every test after two small prompt fixes) — still
  worth a lower `MAX_TOOL_ITERATIONS`-style bound and close testing for
  a multi-step search→fallback→pick→write flow, not fully free-form.
- **Arize Phoenix for profiling/tracing** — self-hosted, OpenTelemetry-
  based, would instrument `api/main.py`'s retrieval and generation calls
  as separate spans. Local-first, consistent with the rest of the stack.
- **Exact equipment filtering, not just semantic** — Tandoor keywords
  (ADR-0015) put equipment tags into the embedded text, so semantic
  search leans toward them but doesn't guarantee an exact match (e.g.
  "духовка" didn't reliably outrank unrelated recipes in one test). A
  Qdrant payload filter on keyword, or a Tandoor API query by keyword,
  would give exact "only recipes I can make with X" filtering if that's
  ever needed instead of fuzzy relevance.
- **Cross-conversation memory for `conversation.velesha`** — right now
  `api/` is stateless per request; within one HA conversation turn HA
  resends the full transcript so the model has short-term memory and can
  in principle phrase a `search_knowledge` query using earlier context,
  but nothing persists once a conversation ends. Deliberately deferred.

## Notes

- Every non-obvious decision gets an ADR in `docs/adr/`, not just a mention
  in a commit message.
- Data sources are UA-heavy (recipes, notes) — embedding model choice
  already accounts for this (bge-m3, see ADR-0002).
