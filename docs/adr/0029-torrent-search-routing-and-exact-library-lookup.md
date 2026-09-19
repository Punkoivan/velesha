# ADR-0029: Toloka search always offered; exact `jellyfin_find`; budget fallback announced

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The user reported that searching for a film on Toloka "doesn't work":
asked about "Ідеальний шторм" the agent answered that none of the
torrents *with zero upload* was that film. Reproduced on the live
server; three separate causes, one of them mine:

1. **Multi-turn routing lost the request.** `toloka_search` was only
   offered when the *latest* message contained a Toloka/torrent word
   (ADR-0024's gate). After a conversation about ratio-0 torrents,
   "а Ідеальний шторм є?" had no such word, so the model continued the
   qBittorrent topic and called `qbittorrent_list`.
2. **Semantic library search cannot say "not found".** For "Ідеальний
   шторм" `search_knowledge` returned the nearest film, «Ураган» (1999),
   and the model reported it as the same film, ignoring a prompt rule not
   to. A prompt is not a guard.
3. **The daily token budget had run out** (500 918 of 500 000, mostly
   my own evaluation runs), so the live agent had silently dropped to the
   local 3B model, which behaves exactly like the user's transcript
   (wrong tool, garbled Ukrainian, tool calls printed as text). This is
   most likely what the user hit first.

## Decision

- **`toloka_search` is always offered** (read-only, one login + one HTTP
  request). The word gate existed for cost, but it inspected only the
  latest message and broke multi-turn use. `toloka_add` keeps its gates
  (fresh search + add verb + user-named category + disk check).
- **Tool descriptions and the prompt say which tool answers which
  question:** `qbittorrent_*` = torrents *already added*; `toloka_search`
  = looking for a *new* release to download; `jellyfin_find` = is it in
  the library. Rule for "знайди / є X / хочу подивитись X": check the
  library first, and only if it says "немає" search Toloka.
- **New `jellyfin_find(title)`**: exact name search through Jellyfin's own
  API (returns real name matches only; explicit "немає … схожі за змістом
  фільми не вважаються збігом" otherwise). Replaces `search_knowledge` for
  "is it in my library".
- **The budget fallback is announced**: when a hosted model is configured
  but the budget is spent, the answer starts with "(Денний ліміт токенів
  вичерпано — відповідає слабша локальна модель.)". A silent downgrade
  looked like the assistant suddenly getting stupid.

## Verification

Same conversations on OpenAI `gpt-4.1-mini` (isolated process, explicit
test budget): "знайди Ідеальний шторм", the same after a ratio-0
conversation, and "хочу подивитись Ідеальний шторм" → `jellyfin_find` →
"немає" → `toloka_search` → the Toloka list (13 seeders, 2.16 GB top
result). "а Ідеальний шторм є?" → "немає у бібліотеці" (a library question,
answered as one). Controls: "скільки роздач з рейтингом 0?" →
`qbittorrent_list`, "є в мене Декстер?" → `jellyfin_find` → "так, 2006".
With the budget spent the live server now prefixes the warning.

## Alternatives considered

- **Keep the gate, widen the word list** — still per-message; the next
  phrasing without the word fails the same way.
- **Look at the whole conversation for cue words** — more logic for a
  read-only tool that is safe to offer always.
- **Threshold on semantic score to detect "not found"** — brittle across
  a multilingual index; the exact API is deterministic.

## Consequences

- `jellyfin_find` only matches names as stored: the library mixes
  English file-style names ("The Mummy") and Ukrainian ones, so a
  Ukrainian query for an English-named film returns "немає" (verified:
  «Мумія» → not found, "Mummy" → three films). The consequence is a
  Toloka search for something already owned; the list shows it, the user
  decides.
- Budget sizing was wrong for a heavy testing day: 500k tokens ≈ 30–50
  agent questions, and evaluation harness runs share the same counter.
  Whether to raise it is the user's cost call (see the summary).
- Always offering `toloka_search` makes a Toloka request each time the
  model chooses it; bounded by the loop stop and `MAX_TOOL_ITERATIONS`.
