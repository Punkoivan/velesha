# ADR-0021: `play_on_jellyfin_device` — the first state-changing tool, gated in code

> Superseded by ADR-0038 (власні Jellyfin-інструменти замінено MCP-сервером; allowlist Kodi збережено як `_jellyfin_guard`).

- **Status**: accepted
- **Date**: 2026-09-19

## Context

"Включи на Jellyfin Декстера на пристрої Kodi" did nothing useful: HA's
own intent matching had switched on the TV's *smart plug* (`switch.tv`,
confirmed via the HA logbook — a user-context `homeassistant.turn_on`;
Velesha's tools were read-only and no request from HA appeared in the API
log). The plug is not the TV: the earlier "TV on" history answers really
meant "the outlet was powered". What HA can't do is start playback: there
is no `media_player` for Kodi in HA (only an empty `scene.kodi`). But
Kodi (the Jellyfin add-on) registers as a remote-controllable Jellyfin
session (`Kodi (192.168.88.99)`, user `tv`), and Jellyfin's API can start
playback on a session.

## Decision

`play_on_jellyfin_device(title)` in `api/tools.py`, backed by a small
`api/jellyfin_client.py` (Jellyfin creds added to `api/secrets.enc.env`,
merged with the HA ones):

1. Search the library (Movies/Series) by title, preferring a name that
   starts with the term.
2. Series → Jellyfin's *Next Up* episode, else the first (verified:
   Декстер → S07E07 «Хімія», consistent with the 75% watched).
3. Find the session whose device/client contains `kodi` and supports
   remote control — a **hard allowlist**, not "any session" (the Firefox
   session is never a target).
4. Wait up to 60 s for that session to appear (Kodi boots after its
   outlet is switched on), then `POST /Sessions/{id}/Playing`.
   Returns a plain message if Kodi isn't online.

Verified for real: Jellyfin reported Kodi now playing «Декстер» S07E07
after the agent call.

**The safety gate is in code, not in the prompt.** First version relied
on the tool description ("only when the user explicitly asks") and it
failed at once: on "що у нас є з Декстера в Jellyfin?" and "порадь
серіал на вечір" the 3B model called the play tool (each request is
stateless, so the "already playing" answers it gave could only come from
having called it). Now `tools.tools_for(last_user_message)` **does not
offer** state-changing tools unless the last user message contains an
explicit command stem (`включ/увімкн/ввімкн/запуст/постав/відтвор/play`),
and the system prompt mentions the tool only in that case. Re-run of the
same four read-only questions: audit log shows only `search_knowledge`,
no play call. Every tool call is now also printed as an audit line
(`TOOL name args`).

## Alternatives considered

- **Prompt wording alone** — tried, failed (above).
- **Confirmation turn ("запустити Декстера? так/ні")** — safer against
  wrong-title picks, but doubles the round trips on a slow CPU model and
  HA's Assist doesn't carry a confirmation state through this API; the
  command-verb gate plus a spoken result ("Запущено … на Kodi") was
  judged enough for a low-stakes action on the user's own TV.
- **HA-side control** (media_player for Kodi) — doesn't exist; Jellyfin's
  session API is the working path.

## Consequences

- Known limits: title search is a Jellyfin substring search in the
  library's own language, so "Мумія" doesn't find "The Mummy" (names are
  English there); "Dexter" and "Декстер" both work. The model sometimes
  calls a series "Фільм" in the reply (cosmetic).
- The command-verb list is a heuristic: a read question that happens to
  contain such a stem ("що запустити?") would offer the tool — the model
  then still has to choose to call it. Acceptable; extend the gate (or
  add confirmation) if a false start ever happens.
- Only Kodi is controllable; adding a device means extending the
  allowlist deliberately.
- Device *power* stays with HA (Assist's own intents), which already
  handles "включи ..." for the outlet.
