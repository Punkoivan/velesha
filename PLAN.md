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
- [x] OpenAI-compatible API (`api/`) — retrieval-grounded
      `/v1/chat/completions` — see ADR-0011
- [x] Wired into Home Assistant as `conversation.velesha`, via HA's
      built-in `llama_cpp` integration (not "OpenAI Conversation" — that
      dropped custom-`base_url` support; see ADR-0013). Streaming
      implemented (HA requires it). Verified end-to-end via
      `/api/conversation/process` — Q&A works; device control/live state
      does not (HA's Assist tool schema is received but not acted on).
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
  vault." Needs two things Velesha doesn't have yet: a web search tool,
  and incremental indexing (today's `index.py` always re-embeds
  everything from scratch). Given the local chat model (Qwen2.5-3B on
  CPU) is not a reliable open-ended tool-use planner, this would likely
  be a fixed orchestration script (search → fallback → LLM picks/
  formats → write) rather than a free tool-calling loop.
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
  resends the full transcript so the chat model has short-term memory,
  but retrieval only ever runs against the latest user message, not the
  conversation so far ("а скільки там калорій" without repeating the
  recipe name won't retrieve the right one), and nothing persists once a
  conversation ends. Deliberately deferred — revisit retrieval-with-
  conversation-context and any longer-term memory together.

## Notes

- Every non-obvious decision gets an ADR in `docs/adr/`, not just a mention
  in a commit message.
- Data sources are UA-heavy (recipes, notes) — embedding model choice
  already accounts for this (bge-m3, see ADR-0002).
