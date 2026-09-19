# ADR-0028: `web_search` tool via OpenAI's Responses API

- **Status**: accepted
- **Date**: 2026-09-19

## Context

Testing in the HA UI: after a Tarantino suggestion the user said "ні,
пошукай в інтернеті щось свіжіше" and the agent answered that it had no
internet access — true: no such tool existed (the gap flagged when the
Tarantino scenario was first discussed). Two candidate providers:

- **Gemini with Google Search grounding.** Attractive: a web search
  query is public by nature (the user's point — nothing private is asked
  when searching the web), so the free-tier data policy is not a problem
  for the query text. **Tested against the real key: not available.**
  Plain `generateContent` calls succeed on `gemini-flash-latest` and
  `gemini-flash-lite-latest`, but the same request with the
  `google_search` tool returns HTTP 429 "exceeded your current quota" on
  every model tried (`flash-latest`, `flash-lite-latest`,
  `3-flash-preview`). Grounding has no free-tier quota; it needs billing
  enabled on the Gemini project (billed per search query).
- **OpenAI web search** in the Responses API (`tools: [{"type":
  "web_search"}]`), on the paid key already in use. Works, billed per
  call on top of tokens.

## Decision

`web_search(query)` in `api/tools.py`, backed by OpenAI's Responses API
with the working model (`CHAT_MODEL`), returning a short Ukrainian answer
(3–6 points with names and years, told today's date) plus up to three
source URLs (tracking parameters stripped).

Controls, all in code:
- **Availability**: only when OpenAI is the working model
  (`CHAT_PROVIDER=openai`), the key exists, the daily web-search cap
  isn't spent and the token budget isn't exhausted. Otherwise the tool
  isn't offered.
- **Trigger gate**: offered only when the message contains an
  internet/freshness cue (`інтернет`, `в мережі`, `погугли`, `свіж`,
  `нов…`, `останн`, `вийшл`, `imdb`, `рейтинг`, a recent year, …) — the
  same "code decides what is offered" pattern as the action tools, here
  for cost rather than safety.
- **Daily cap** `DAILY_WEB_SEARCHES` (default 15), persisted next to the
  token budget in `api/data/usage.json`; tokens used count against the
  daily token budget too.
- **Query masking**: the query passes through `guardrails.redact` before
  it leaves; the tool description tells the model to phrase queries
  without personal data. Results are public web text.

Verified: direct call returned real, sourced results (counter 15 → 14);
the user's exact two-turn conversation ("хочу подивитись фільм Тарантіно"
→ list → "ні, пошукай в інтернеті щось свіжіше") offered `web_search`,
called it with `нові фільми Квентіна Тарантіно 2026`, and answered with
current projects in 9 s.

## Alternatives considered

- **Gemini grounding after enabling billing** — the better fit in
  principle (native Google Search, and a paid project isn't used for
  training); a config change plus a second code path if the user wants
  it. Not done: it needs a billing decision the user hasn't made.
- **A plain search API (Brave/Tavily/SerpAPI) + summarizing with the
  working model** — likely cheaper per query and provider-independent,
  but another account/key and a summarization step; kept as the option
  if per-call OpenAI pricing bites.
- **Ungated availability** — a chatty model could spend the cap on
  questions it can answer itself.

## Consequences

- Web search costs per call (check the provider's current pricing; not
  recorded here because it changes) — bounded by the daily cap and the
  token budget, both configurable.
- The gate is a heuristic: "щось свіжіше" alone, without an internet
  cue and without an earlier internet word in the *latest* message,
  won't offer the tool (only the latest user message is inspected).
- Search answers come from the provider's search+summary, not a source
  list the agent verified; the URLs are shown so the user can check.
- The Tarantino chain is now buildable end to end (suggest → search the
  web → check Jellyfin → find on Toloka → add); it has not been tested
  as one flow yet.
