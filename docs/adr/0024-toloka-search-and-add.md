# ADR-0024: Toloka.to search and add-from-.torrent

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The user wanted to add torrents beyond magnets, ideally from toloka.to
(a private Ukrainian tracker with no API) and have the agent do the rest.
The first plan (give a topic URL, agent infers the category from the
forum section) was **corrected by the user**: they give a film/series
*title*, the agent searches Toloka with their login and returns options
with **title, size and seeder count**, and the user names the section
(category) themselves.

Checked the site before designing: reachable by scripts (no Cloudflare or
captcha), classic `login.php` form (`username`/`password`/`login`), and
the logged-in search page (`tracker.php?nm=…`) already carries, per
result row, the forum section, title, direct `download.php?id=…` link,
size, seeders, leechers and date — so no topic page needs opening.

## Decision

`api/toloka_client.py` (HTML scraping with BeautifulSoup, the only new
dependency; credentials in `api/secrets.enc.env`; lazy login, re-login
when the "logout" marker is missing; talks only to `https://toloka.to`):
`search(query)` and `download_torrent(id)` (which refuses anything not
shaped like a bencoded `.torrent`, e.g. an HTML login page). A parse that
finds result cells but no rows raises loudly instead of returning
nothing. `qbit_client.add_file` uploads the `.torrent` bytes
(`torrents/add`, multipart) with `autoTMM=true`.

Two tools, in a two-step flow:

- **`toloka_search(query)`** — up to 8 variants sorted by seeders, each
  numbered with title, size, seeders, forum section; the footer shows
  free disk space and the available categories. Offered only when the
  message mentions Toloka/torrents (stem regex `толо[кцзч]` — the first
  version missed «на толоці», a case form where the stem alternates
  к/ц). **Its output is returned verbatim, with no second model call**
  (`PASSTHROUGH_TOOLS`): the numbered list is what the user refers back
  to, the 3B model rewrites and drops lines, and skipping the call saves
  most of a minute on this CPU.
- **`toloka_add(number, category)`** — adds a row of the *last search*
  (cached server-side for 30 min; single-user assistant), not a
  model-supplied URL. Offered only when a fresh search exists and the
  message has an add verb. Guards, all in code: number in range;
  category limited to those with a save path; **category must be
  corroborated by the user's own words** (first 5 letters in the
  message; the model guessed «фільми» when none was given and was
  refused — nothing added); and a **disk-space check**.

The disk check came out of the first real search: the top hit for
«Декстер» was 196.56 GB with 37.7 GB free. Results that won't fit are
marked "НЕ ВЛІЗЕ на диск" in the list and `toloka_add` refuses them
("НЕ ДОДАНО. Не вистачає місця…"), keeping a 5 GB margin.

## Verification

Real login and search (29 results for «Dexter»), real `.torrent`
download (28 KB, valid bencode), empty query → 0 rows. End to end
through the agent with the add step run in `stopped` mode (test only,
so the tracker is never contacted and nothing downloads): search listed
correctly; «додай 2» without a category → refused (model had guessed
«фільми»); «додай 2 в серіали» → torrent appeared with category
`серіали`, `save_path /share/qBittorrent/series`, state `stoppedDL`,
then deleted (count back to 99). The 196 GB variant was refused for
space with the torrent count unchanged. Live HTTP search «Mandy 2018»
returned 7 variants.

## Alternatives considered

- **Infer the category from the forum section** (first plan) — rejected
  by the user; also the sections don't map 1:1 (results included
  "Архів відео", "АртХаус", even a book).
- **Let the model pass a topic URL** — cannot be verified against
  anything the user showed; a cached numbered list can.
- **Second model call to phrase the results** — worse and slower for
  this output.

## Consequences

- Scraping is brittle by nature: a Toloka redesign breaks the parser;
  it fails loudly (empty-result vs. unparsed distinguished) rather than
  guessing.
- Downloading a `.torrent` counts against the user's Toloka account like
  a manual download; the agent does it only on an explicit add.
- Search results include non-video items (books, music); the user picks
  by number, the section shown in the list helps.
- The real add path starts the download immediately (`start=True`); only
  the test harness used stopped mode.
- Cache is process-local: restarting the API loses the "last search",
  so an add right after a restart is refused until a new search.
