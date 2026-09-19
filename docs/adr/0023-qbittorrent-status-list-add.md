# ADR-0023: qBittorrent tools — status, zero-ratio lists, add with a category

- **Status**: accepted
- **Date**: 2026-09-19

## Context

qBittorrent runs as an HA add-on (`http://ha.punka.space:8081`, v5.2.3;
HA sees only an `update` entity for it, so the direct Web API is the way,
as for Jellyfin/Tandoor). The user wants: uploaded this session, uploaded
all-time, how many seeds have ratio 0, and to add torrents with metadata
because the metadata decides which directory they land in.

Real data shaped the design: 99 torrents, `up_info_data` (session) 330.8
GB vs `alltime_ul` 5.24 TB, global ratio 4.77, 40 GB free. **Ratio 0 is
ambiguous** — 5 completed torrents have `ratio == 0` (never uploaded),
28 completed ones uploaded nothing *this session* (ratio > 0 overall) —
so both are reported and named separately. Categories: `фільми`,
`серіали`, `мультики` have save paths; `movies` (a duplicate of
`фільми`) and `Книги` have none, and global Automatic Torrent Management
is off — so a category only routes files if the add call passes
`autoTMM=true` *and* the category has a path.

## Decision

`api/qbit_client.py` (lazy login, re-login on 403; credentials in
`api/secrets.enc.env`) and three tools:

- **`qbittorrent_status`** — session vs all-time upload/download as
  separate labelled figures, global ratio, speeds (MB/s), free space,
  counts (downloading/paused/errors), and both zero counts. All
  arithmetic in code (ADR-0017/0020 rule).
- **`qbittorrent_list(filter)`** — `zero_ratio | zero_session |
  downloading | problems`, up to 10 rows with ratio, uploaded this
  session, uploaded total, category, size. Only completed torrents count
  as "zero" (a downloading one is naturally 0).
- **`qbittorrent_add(url, category)`** — state-changing, so gated in code
  like ADR-0021, with three extra guards because a wrong category puts
  files in the wrong folder: (1) the offered tool exists only when the
  user's own message contains a `magnet:`/`http(s)://` link, and the
  tool rejects a URL that isn't verbatim in that message (a model can't
  invent one); (2) `category` is an enum built live from qBittorrent
  restricted to categories **with a save path**; (3) the category must
  be corroborated by the user's text (its first 5 letters appear in the
  message) — otherwise the tool returns "НЕ ДОДАНО … запитай категорію".
  Adds with `autoTMM=true` so the file goes to the category's directory.
  No delete tool, no pause/resume (not asked for).

## Verification

Read tools on real data (5 zero-ratio, 28 zero-session, 330.8 GB / 5.24
TB). Add, in-process: no category word → refused; invented URL →
refused; category without a path (`Книги`) → refused; real add of a
public test torrent (Big Buck Bunny) with «мультики» → landed in
`/share/qBittorrent/cartoon` with `auto_tmm: true`, then deleted with its
files by hash (count back to 99). Through the agent: with «в мультики»
it added correctly; **without** a category the model guessed «фільми»
twice and the guard rejected both (nothing added) — confirmed the guard
is what protects the folder, not the prompt. That guard message
originally said only "уточни", and the model's reply then read as if it
had added the torrent ("Додам … до 'фільми'"); the message now starts
with "НЕ ДОДАНО".

Timeouts: the extra tool schemas lengthened the prompt enough that a
model call exceeded 120 s once and returned a 500 to the client. The
per-call timeout is now 180 s and any `RequestException` becomes a
plain "Модель не встигла відповісти — спробуй ще раз."

## Consequences

- Reading is robust; the 3B model still misreads sometimes (e.g. "які
  торренти нічого не віддали?" → tool returned 28, model said none).
  Data is right; the stronger-model option stays open (ADR-0022).
- The duplicate `movies` category (and `Книги`) has no path: it can hold
  torrents but can't be targeted by the agent. Give it a path in
  qBittorrent or merge into `фільми` if desired; the user's existing
  `Mandy` torrent still sits in `movies`.
- Adding is text-only (a magnet must appear in the message) — fine for
  the HA chat UI, not for voice.
