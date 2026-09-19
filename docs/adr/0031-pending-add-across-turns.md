# ADR-0031: Remember a picked variant across turns; category-only replies

- **Status**: accepted
- **Date**: 2026-09-19

## Context

After ADR-0030 the user tried again with a natural conversation, spoken
or typed in separate short messages, and it still failed at first:

1. "Хочу подивитись Ідеальний шторм" → Toloka list.
2. "завантажуй перши" → the model called `toloka_add` with the category
   «фільми» **it had guessed**; the category guard (ADR-0023/0024)
   correctly refused, and the agent asked which category.
3. "фільми" → **no add tool offered**: this message has no add verb, no
   digit and no ordinal, so the per-message gate (ADR-0030) dropped the
   tool and the model wandered off to `qbittorrent_list`.

Only "1, фільми" (number and category in one message) finally worked —
exactly the case the gate handled. The design assumed each message
carries its own intent; real users (and STT-split voice) answer a
question with one word.

## Decision

- **Server-side pending pick.** When `toloka_add` refuses because the
  category is missing or unknown but the *number* is valid, the pick is
  remembered (`_pending_add`, 10 min TTL, single-user assistant). The
  variant list itself is remembered for 30 min (ADR-0024).
- **Gate**: the add tool is offered when a fresh search exists and the
  message has an add verb/number/ordinal (ADR-0030) **or** a pending pick
  exists **or** the message names one of the categories that have a save
  path (first 5 letters, same rule as the guard).
- **`number` is optional** in the tool schema; when omitted and a pending
  pick exists, it is used. The prompt tells the model: if the user already
  chose a variant and now only names a category, call `toloka_add` with
  that category. A successful add clears the pending pick.
- The guards do not move: the category must appear in the user's own
  message (a model guess is refused and only sets the pending pick), the
  number must be in range, the disk check applies, the outcome guard
  (ADR-0030) flags claims of actions that did not happen.

## Verification

Replayed the failing conversation on `gpt-4.1-mini` with a different
film ("Mandy 2018") in `stopped` test mode: search → "завантажуй другий"
(add tool offered, agent asks for the category, nothing added) → bare
"фільми" (tool offered via the pending pick, `acted: True`) → torrent
present in category `фільми`, `save_path /share/qBittorrent/movies`;
removed afterwards. "дякую" does not open the add tool. The user's real
"Ідеальний шторм" download (17% at the time of checking, 2.16 GB) was
added by their own successful retry with "1, фільми".

## Alternatives considered

- **Ask for number and category together every time** — pushes the
  system's limitation onto the user.
- **Offer the add tool whenever a fresh search exists** — see ADR-0030;
  guards would still hold, but every stray message becomes an add attempt.
- **Read the whole conversation for the pick** — HA passes only user and
  assistant *text*; the tool call with its number is not in the history,
  so server-side state is the reliable carrier.

## Consequences

- State lives in process memory: a restart between the question and the
  answer loses the pending pick (the agent then asks again).
- A pending pick is not tied to a conversation id; in a single-user setup
  this is fine, with several concurrent users it would mix picks.
- A category word in an unrelated message within 30 minutes of a search
  ("а серіали є?") opens the tool; it still cannot add without a valid
  pick or pending pick.
- While fixing this I replaced a code range too broadly and deleted two
  unrelated tools (`jellyfin_find`, `web_search`) from the working copy;
  the running server was unaffected, the code was restored from the last
  commit and re-verified before this commit.
