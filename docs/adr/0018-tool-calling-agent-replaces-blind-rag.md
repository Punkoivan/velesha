# ADR-0018: `api/` becomes a tool-calling agent, replacing always-on blind RAG

- **Status**: accepted
- **Date**: 2026-09-19

## Context

Three real questions, in a row, exposed the same structural limit of
the retrieve-then-generate pipeline from ADR-0011/0016/0017:

1. "чи зараз відчинені вхідні двері?" (live state) — RAG only ever sees
   an indexed *snapshot*, never "right now"; the door sensor in question
   wasn't even indexed at all (a separate HA-side naming mess, left
   alone per the user's own call).
2. "коли востаннє було вімкнено телевізор?" (recency over history) —
   fixed in ADR-0017 by retrieving a wider pool and sorting by real
   timestamp, but still fundamentally "hope the right facts are in the
   blindly-injected context."
3. "скільки 18.09 числа пралка використала електроенергії?" (a computed
   delta over a numeric time series) — this is not a retrievable fact at
   all. No amount of better retrieval finds a pre-written sentence
   answering "how many kWh did the washing machine use on date X" for
   an arbitrary X; it has to be *computed* from raw history at query
   time. Ground-truth checked by hand against the HA history API:
   0.55 kWh (435.55 - 435.00 kWh counter values that day).

All three need the model to actively *decide* what data it needs and
fetch it live, not have the same fixed context blindly prepended to
every question regardless of whether that question is even answerable
that way. This is exactly the tool-calling agent direction flagged as
future work back when `api/` was first built (ADR-0011) and reinforced
by the borscht-recipe example earlier in `PLAN.md`'s "Future ideas" —
the difference is these three bugs made the need concrete rather than
hypothetical.

## Decision

Rebuilt `api/main.py` as a tool-calling agent loop instead of a fixed
retrieve-then-generate pipeline. Confirmed first that the underlying
model/server combination actually supports OpenAI-style tool calling
(`finish_reason: "tool_calls"` from `llama-server` with `--jinja` and
Qwen2.5-3B-Instruct, verified directly before building anything on top)
— this was a real risk given earlier skepticism in this project about a
quantized 3B CPU model's reliability as an open-ended planner.

**Three tools**, chosen to close exactly the three gaps above, no more:

- `search_knowledge(query, source?)` — semantic search across
  `tandoor_recipes` / `ha_history` / `jellyfin_library` (the old
  `retrieve_context` logic, now callable instead of automatic; lives in
  `api/tools.py`, backed by `api/search_backend.py` which both
  `main.py` and `tools.py` import — a plain shared module, not
  duplicated, since both are the same `uv` project unlike the deliberate
  cross-directory duplication elsewhere, ADR-0011).
- `get_live_state(entity_name)` — direct HA `/api/states` lookup via a
  new `api/ha_client.py` (own copy of `HA_URL`/`HA_TOKEN`, same instance
  as `sources/home_assistant/`, its own `api/secrets.enc.env`).
- `get_energy_usage(entity_name, date)` — direct HA history query,
  computes `max(values) - min(values)` over the given calendar day for
  a `kWh`-unit sensor.

**Entity resolution** (`tools.resolve_entity`) fuzzy-matches a natural-
language hint against every state's `friendly_name`, filtering out
known diagnostic noise (`identifikuvati`, `firmware`, `child_lock`,
`power_on_state`, `backlight_mode`, `battery`) and, on score ties,
preferring "primary" domains (`switch`/`light`/`climate`/`binary_sensor`/
etc.) over `sensor` — needed because a bare device name like
"телевізор" matches several entities equally well (the switch itself,
plus its Струм/Напруга/Summation-delivered diagnostic sensors), and
without this tie-break `get_live_state("телевізор")` picked the
cumulative energy counter instead of the on/off switch.

**Iteration bound**: `MAX_TOOL_ITERATIONS = 4` — CPU-only generation is
slow enough (ADR-0010) that an unbounded tool loop is a real latency
risk, not just a correctness one.

**Two model-specific fixes, found only by testing against the real
model, not designed in advance:**

- **Date grounding**: the model has no notion of "today" — asked for
  energy usage on "18.09" with no year, it called `get_energy_usage`
  with `"date": "2022-09-18"` (a training-era guess). Fixed by injecting
  today's actual date into the system prompt on every request
  (`system_prompt()`, not a static string).
- **Repetition loops**: a recipe-recommendation answer degenerated into
  repeating "Додайте оливкову олію і нарізайте ..." many times over.
  Fixed with `repeat_penalty: 1.15` on every chat completion request — a
  known quantized-small-model failure mode, not a tool-calling-specific
  bug, but only showed up once answers got longer than the old fixed-
  template RAG answers tended to be.

**Streaming**: HA's `llama_cpp` integration still always sends
`stream: true` (ADR-0013). True token-by-token proxying doesn't fit an
agent loop — tool calls happen server-side across multiple internal,
non-streamed requests to the chat model before there's a final answer
to show. `chat_completions` now always runs the full agent loop
internally, then — if the caller asked for streaming — wraps the
complete final answer as a single properly-framed SSE chunk plus
`[DONE]` (`sse_chunk`). Verified working against HA's actual client, not
just `curl`.

## Alternatives considered

- **Keep blind RAG, add narrow patches per bug** (e.g. a special case for
  "коли" questions, another for "скільки кВт·год") — this is the path
  ADR-0016/0017 were already on, and it doesn't scale: every new
  question shape needs its own bespoke retrieval logic baked into
  `retrieve_context`. Tool calling lets the model itself decide which
  capability a question needs, from a fixed, small toolset — new
  capabilities are new tools, not new retrieval special-cases.
- **A bigger/better local model for more reliable planning** — real
  option if tool-calling accuracy turns out to be the bottleneck in
  practice, but not needed yet: Qwen2.5-3B chose the right tool with
  correct arguments in every test after the date-grounding and entity-
  resolution fixes, aside from one residual mistake (see Consequences).
- **True per-tool-call streaming** (stream the model's tool-call
  reasoning, not just the final answer) — meaningfully more complex for
  a benefit (perceived responsiveness) that doesn't matter much for a
  voice/text assistant answering in a few seconds to ~1.5 minutes;
  deferred.

## Consequences

- `api/` now needs three processes' worth of dependencies at runtime:
  embedding server (8083), chat server (8084, tool-calling capable), and
  its own two-level secret chain (root Qdrant secret + `api/`'s own HA
  copy) — one more moving part than the pre-agent version.
- Not perfect: one test ("коли востаннє було вімкнено телевізор?" via
  `search_knowledge`) still picked the second-most-recent event instead
  of the most recent, even though the tool's own output was correctly
  sorted with the right answer first — a residual 3B-model reading-
  comprehension error, not a retrieval bug (confirmed by calling the
  tool directly and inspecting its output). Acceptable for now; revisit
  the "bigger/better local model" alternative if this class of mistake
  recurs often.
- `retrieve_context` (the always-on blind pipeline) is gone from
  `api/main.py` entirely — `cli/ask.py` still has its own, separate
  retrieve-then-generate implementation (ADR-0010/0017) and was **not**
  changed by this ADR; it remains a direct-answer CLI tool, not an
  agent. Revisit only if the CLI path needs the same tool-calling
  capabilities later.
- The HA device-control tool schema HA sends (`intent__HassTurnOn`
  etc., ADR-0013) is still received and still ignored — this ADR adds
  read-only tools (search, live state, energy), not device control.
  Still explicitly out of scope, same as before.
