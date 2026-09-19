# ADR-0030: Wider add-intent gate; catching claimed-but-not-performed actions

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The user searched Toloka, picked a variant, and the assistant said it had
added it — but nothing appeared in qBittorrent. The agent audit log showed
**no `toloka_add` call at all** for the request ("давай перший варіант,
категорія фільми.", 21:23:41). Two defects:

1. **The gate was too narrow.** `toloka_add` (ADR-0024) is offered only
   when a fresh search exists *and* the message has an add verb
   (`додай|додати|завантаж|скач|качай|постав`). "давай перший варіант"
   has none, so the tool was simply not offered — while the conversation
   history still described it.
2. **Nothing stopped the model from claiming success anyway.** With the
   tool absent, `gpt-4.1-mini` answered as if it had run ("додано…").
   For an action tool that is worse than an error: the user believes a
   download is running.

## Decision

- **Add-intent gate widened** to selecting from the list just shown:
  `давай`, `бери`, `візьми`, `обер/вибер`, `варіант`, `номер`, `№`, a
  digit 1–8, ordinals (`перш/друг/трет/четвер/п'ят`) in addition to the
  add verbs. Being broad is acceptable because the tool stays behind its
  other code guards: a *fresh search* (30 min), the **category named in the
  user's own words**, the number in range, and the disk-space check.
- **Unfounded-claim guard** (`main.unfounded_claim`): every action tool's
  success message starts with a known prefix (`Запущено`, `Додано`,
  `Поставлено на паузу`, `Продовжено`, `Зупинено`); `run_agent` records
  whether any action tool *succeeded* in this request. If the final
  answer contains an action-claim word (`додав/додано/запустив/запущено/
  поставлено на паузу/продовжено/зупинено`…) and none did, the answer is
  suffixed with "⚠ Насправді нічого не змінено: жодну дію не виконано.
  Скажи ще раз, що саме зробити." A code check on the outcome, not a
  prompt request.

## Verification

Guard unit checks (claim without action → warns; with a successful action,
without a claim, and on a read answer → silent). Gate: the user's exact
phrase, "бери 2 у фільми", "додай 1 в фільми" offer the tool; an unrelated
question does not. Replay of the conversation on `gpt-4.1-mini` with the
add run in `stopped` test mode: search → "давай перший варіант" (no
category) → the agent asks which category; "давай перший варіант,
категорія фільми." → `toloka_add {"number":1,"category":"фільми"}` →
torrent present with category `фільми`, `save_path
/share/qBittorrent/movies`; removed afterwards, count back to 99.

## Alternatives considered

- **Always offer `toloka_add` when a fresh search exists** — simplest,
  but then any chat ("дякую", "а що ще є?") could trigger an add attempt;
  the category/number/disk guards would catch most, at the price of noisy
  refusals. The wider verb list keeps intent explicit.
- **Rely on the prompt ("don't claim actions you didn't perform")** — the
  failure was the model ignoring that class of instruction; a guard on
  the outcome does not depend on obedience.
- **Regex-strip the claim instead of appending a warning** — could
  mangle a correct answer; appending is honest and can't make it worse.

## Consequences

- The claim guard is a word heuristic: a fluent hallucination that avoids
  the listed words ("готово, качається") passes; conversely a phrase such
  as "раніше я додав…" would be flagged when no action ran this request
  (false positive, harmless: an extra warning). Extend the list as real
  cases show up.
- A digit in a message ("2 фільми на вечір") opens the add tool after a
  search — still needs the category words and a fresh search to do
  anything.
- The user's film ("Ідеальний шторм", first variant) was **not** added by
  this session; the original request only ever produced a claim. They
  repeat the command in the UI.
