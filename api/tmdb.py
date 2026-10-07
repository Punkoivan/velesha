"""TMDB: identify watched titles and find what the household hasn't seen yet (ADR-0075).

v3 API key (TMDB_API_KEY). Ukrainian titles/overviews via language=uk-UA.
"""

import os
import re
import sqlite3

import requests

import watch_history

_API = "https://api.themoviedb.org/3"
_LANG = "uk-UA"
_genres_cache: dict[str, dict[str, int]] = {}


def available() -> bool:
    return bool(os.environ.get("TMDB_API_KEY"))


class TmdbError(RuntimeError):
    """An HTTP failure WITHOUT the request URL: it carries api_key/session_id (ADR-0079)."""


def _get(path: str, **params) -> dict:
    r = requests.get(f"{_API}{path}", params={"api_key": os.environ["TMDB_API_KEY"], "language": _LANG, **params},
                     timeout=20)
    if not r.ok:
        raise TmdbError(f"TMDB {path}: HTTP {r.status_code}")
    return r.json()


# Release-name noise in folder/file names: "Barry (S01-04) (2018-2023) WEB-DLRip-AVC [2xUkr Eng]"
_NOISE = re.compile(r"\[[^\]]*\]|\((?!\d{4}\))[^)]*\)|\b(S\d{2}(-\d{2})?|Season\s*\d+(-\d+)?|сезон\s*\d+|"
                    r"\d{3,4}p|WEB-?DL\w*|WEBRip|BDRip\w*|DVDRip\w*|HDTV\w*|BluRay|x26[45]|HEVC|AVC|DVD\d?|"
                    r"SatRip|XviD|Hurtom|Ukr|Eng)\b", re.IGNORECASE)


def clean_title(title: str) -> tuple[str, int | None]:
    title = title.replace("_", " ")
    year = None
    m = re.search(r"\((\d{4})\)", title)
    if m:
        year = int(m.group(1))
    t = _NOISE.sub(" ", title)
    t = re.sub(r"\(\d{4}(-\d{4})?\)", " ", t)
    t = re.sub(r"^\d+[a-z]?\.\s*", "", t.strip())  # "02. The Mummy Returns", "12b. Friday the 13th"
    t = re.sub(r"(?<=\w)\.(?=\s)", " ", t)  # "Hostel. Part II" — TMDB search chokes on the dot
    t = re.sub(r"[\s.\-_]+$", "", re.sub(r"\s+", " ", t)).strip()
    return t, year


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def identify(title: str, year: int | None = None, kind: str = "unknown") -> dict | None:
    """Best TMDB match for a title (any language): exact title match first, then most voted."""
    q, y = clean_title(title)
    year = year or y
    if not q:
        return None
    results = _get("/search/multi", query=q).get("results") or []
    results = [r for r in results if r.get("media_type") in ("movie", "tv")
               and (kind == "unknown" or r["media_type"] == {"movie": "movie", "series": "tv"}[kind])]
    if not results:
        return None

    def names(r):
        return {_norm(r.get(k)) for k in ("title", "original_title", "name", "original_name") if r.get(k)}

    def ryear(r):
        d = r.get("release_date") or r.get("first_air_date") or ""
        return int(d[:4]) if d[:4].isdigit() else None

    exact = [r for r in results if _norm(q) in names(r)]
    pool = exact or results
    if year:
        pool = [r for r in pool if ryear(r) and abs(ryear(r) - year) <= 1] or pool
    best = max(pool, key=lambda r: r.get("vote_count") or 0)
    kind = "movie" if best["media_type"] == "movie" else "series"
    ext = _get(f"/{best['media_type']}/{best['id']}/external_ids")
    return {"kind": kind, "tmdb_id": str(best["id"]), "imdb_id": ext.get("imdb_id") or None,
            "title": best.get("original_title") or best.get("original_name"), "year": ryear(best),
            "uk_title": best.get("title") or best.get("name")}


def resolve_history() -> dict:
    """Give every watched row without TMDB id one, merging rows that turn out to be the same title."""
    c = watch_history._conn()
    rows = c.execute("SELECT key, kind, title, year, plays, first_watched, last_watched, raw FROM watched "
                     "WHERE tmdb_id IS NULL OR tmdb_id = ''").fetchall()
    done = failed = merged = dropped = 0
    for key, kind, title, year, plays, first, last, raw in rows:
        # An episode file Jellyfin filed as a "movie": "S03E08. The Boys", "True.Detective.s03e05",
        # "S01E18.Beware the Gray Ghost". Its series, when the name carries one, gets the plays;
        # a bare episode title can't be traced — its series is in the history on its own anyway.
        ep = re.search(r"(?i)\bs\d{1,2}e\d{1,3}\b", title)
        if ep:
            before, after = title[:ep.start()], title[ep.end():]
            series = clean_title(before)[0] or (clean_title(after)[0] if re.match(r"^\.\s", after) else "")
            info = identify(series, None, "series") if series else None
            if not info:
                c.execute("DELETE FROM watched WHERE key=?", (key,))
                dropped += 1
                continue
            title, kind, raw = series, "series", None
        try:
            # title = the series/film as parsed; raw may carry "Серіал - Епізод" and only helps as a fallback
            info = identify(title, year, kind) or (identify(raw, year, kind) if raw and raw != title else None)
        except Exception as e:
            print(f"tmdb identify error {title!r}: {e}", flush=True)
            info = None
        if not info:
            failed += 1
            continue
        new_key = watch_history._key(info)
        other = c.execute("SELECT plays, first_watched, last_watched FROM watched WHERE key=?", (new_key,)).fetchone()
        if other and new_key != key:
            c.execute("UPDATE watched SET plays=?, first_watched=?, last_watched=? WHERE key=?",
                      (other[0] + plays, min(other[1], first), max(other[2], last), new_key))
            c.execute("DELETE FROM watched WHERE key=?", (key,))
            merged += 1
        else:
            c.execute("UPDATE watched SET key=?, kind=?, title=?, year=?, imdb_id=?, tmdb_id=?, resolved=1 WHERE key=?",
                      (new_key, info["kind"], info["title"], info["year"], info["imdb_id"], info["tmdb_id"], key))
        done += 1
    c.commit()
    c.close()
    return {"identified": done, "merged": merged, "not_found": failed, "episode_files_dropped": dropped}


def watched_tmdb_ids() -> set[str]:
    c = watch_history._conn()
    ids = {r[0] for r in c.execute("SELECT tmdb_id FROM watched WHERE tmdb_id IS NOT NULL AND tmdb_id != ''")}
    c.close()
    return ids


def genres(media: str) -> dict[str, int]:
    if media not in _genres_cache:
        _genres_cache[media] = {g["name"].lower(): g["id"] for g in _get(f"/genre/{media}/list").get("genres", [])}
    return _genres_cache[media]


def discover(kind: str = "movie", genre: str = "", year_from: int | None = None, year_to: int | None = None,
             count: int = 10) -> list[dict]:
    """Well-rated titles of a genre/years the household hasn't watched, best rated first."""
    media = "tv" if kind == "series" else "movie"
    # Enough votes to be a film people know, not a niche title with a few hundred fans voting 9+.
    params = {"sort_by": "vote_average.desc", "vote_count.gte": 1000 if media == "movie" else 300,
              "include_adult": "false"}
    if "мульт" not in genre.lower() and "аніме" not in genre.lower():
        params["without_genres"] = "16"  # animation/anime carries "бойовик" too — only when asked for
    if genre:
        g = genres(media)
        wanted = [gid for name, gid in g.items() if genre.lower()[:5] in name or name[:5] in genre.lower()]
        if not wanted:
            raise ValueError(f"невідомий жанр «{genre}»; є: {', '.join(g)}")
        params["with_genres"] = "|".join(str(x) for x in wanted)
    date = "primary_release_date" if media == "movie" else "first_air_date"
    if year_from:
        params[f"{date}.gte"] = f"{year_from}-01-01"
    if year_to:
        params[f"{date}.lte"] = f"{year_to}-12-31"
    seen = watched_tmdb_ids()
    liked, disliked, rated_ids = taste(media)
    seen |= {str(i) for i in rated_ids}
    # gather more than asked, then reorder by the household's taste (ADR-0076)
    want = count * 3 if liked or disliked else count
    out: list[dict] = []
    for page in range(1, 8):
        data = _get(f"/discover/{media}", page=page, **params)
        for r in data.get("results") or []:
            if str(r["id"]) in seen:
                continue
            # No Ukrainian overview = no Ukrainian release worth recommending; Russian productions are out
            if not r.get("overview") or r.get("original_language") == "ru":
                continue
            d = r.get("release_date") or r.get("first_air_date") or ""
            score = (r.get("vote_average") or 0) + liked.get(r["id"], 0) - 1.5 * disliked.get(r["id"], 0)
            out.append({"tmdb_id": r["id"], "title": r.get("title") or r.get("name"),
                        "original": r.get("original_title") or r.get("original_name"), "year": d[:4],
                        "rating": r.get("vote_average"), "overview": (r.get("overview") or "").split(". ")[0],
                        "score": score, "because_liked": liked.get(r["id"], 0)})
            if len(out) >= want:
                break
        if len(out) >= want or page >= (data.get("total_pages") or 1):
            break
    out.sort(key=lambda x: -x["score"])
    return out[:count]


# --- household account: ratings and taste (ADR-0076) -------------------------------------------
_account: dict = {}
_recs_cache: dict[tuple[str, int], tuple[float, set[int]]] = {}


def can_rate() -> bool:
    return available() and bool(os.environ.get("TMDB_SESSION_ID"))


def _sid() -> dict:
    return {"session_id": os.environ["TMDB_SESSION_ID"]}


def _account_id() -> int:
    if "id" not in _account:
        _account["id"] = _get("/account", **_sid())["id"]
    return _account["id"]


def rate(media_kind: str, tmdb_id: str, rating: float) -> float:
    """TMDB accepts 0.5–10 in 0.5 steps; returns the value actually stored."""
    value = min(10.0, max(0.5, round(float(rating) * 2) / 2))
    media = "tv" if media_kind == "series" else "movie"
    r = requests.post(f"{_API}/{media}/{tmdb_id}/rating", params={"api_key": os.environ["TMDB_API_KEY"], **_sid()},
                      json={"value": value}, timeout=20)
    r.raise_for_status()
    return value


def rated() -> list[dict]:
    """Every movie and series rated on the account, newest first."""
    out = []
    for media, kind in (("movies", "movie"), ("tv", "series")):
        page = 1
        while True:
            data = _get(f"/account/{_account_id()}/rated/{media}", page=page, sort_by="created_at.desc", **_sid())
            for r in data.get("results") or []:
                d = r.get("release_date") or r.get("first_air_date") or ""
                out.append({"kind": kind, "tmdb_id": r["id"], "title": r.get("title") or r.get("name"),
                            "year": d[:4], "rating": r.get("rating")})
            if page >= (data.get("total_pages") or 1):
                break
            page += 1
    return out


def _recommended_ids(media: str, tmdb_id: int) -> set[int]:
    import time as _t
    hit = _recs_cache.get((media, tmdb_id))
    if hit and _t.time() - hit[0] < 3600:
        return hit[1]
    ids = {r["id"] for r in (_get(f"/{media}/{tmdb_id}/recommendations").get("results") or [])}
    _recs_cache[(media, tmdb_id)] = (_t.time(), ids)
    return ids


def taste(media: str) -> tuple[dict[int, int], dict[int, int], set[int]]:
    """(liked-neighbour counts, disliked-neighbour counts, already rated ids) for one media type."""
    if not can_rate():
        return {}, {}, set()
    kind = "series" if media == "tv" else "movie"
    mine = [r for r in rated() if r["kind"] == kind]
    liked, disliked = {}, {}
    for r in [r for r in mine if (r["rating"] or 0) >= 7.5][:12]:
        for i in _recommended_ids(media, r["tmdb_id"]):
            liked[i] = liked.get(i, 0) + 1
    for r in [r for r in mine if (r["rating"] or 10) <= 5][:8]:
        for i in _recommended_ids(media, r["tmdb_id"]):
            disliked[i] = disliked.get(i, 0) + 1
    return liked, disliked, {r["tmdb_id"] for r in mine}


# --- a film from a link (ADR-0079) ------------------------------------------------------------------
_UA = {"User-Agent": "Velesha/1.0 (home assistant; film lookup)"}


def details(kind: str, tmdb_id: str) -> dict:
    media = "tv" if kind == "series" else "movie"
    d = _get(f"/{media}/{tmdb_id}")
    date = d.get("release_date") or d.get("first_air_date") or ""
    return {"kind": kind, "tmdb_id": str(tmdb_id), "uk_title": d.get("title") or d.get("name"),
            "title": d.get("original_title") or d.get("original_name"), "year": int(date[:4]) if date[:4].isdigit() else None,
            "imdb_id": d.get("imdb_id")}


def _from_imdb(imdb_id: str) -> dict | None:
    found = _get(f"/find/{imdb_id}", external_source="imdb_id")
    for key, kind in (("movie_results", "movie"), ("tv_results", "series")):
        if found.get(key):
            return details(kind, found[key][0]["id"])
    return None


def _from_wikipedia(lang: str, page: str) -> dict | None:
    """Wikipedia article -> its Wikidata item -> TMDB / IMDb ids (P4947 film, P4983 series, P345 IMDb)."""
    r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=_UA, timeout=20, params={
        "action": "query", "prop": "pageprops", "titles": requests.utils.unquote(page).replace("_", " "),
        "redirects": 1, "format": "json"}).json()
    qid = next((p.get("pageprops", {}).get("wikibase_item") for p in r.get("query", {}).get("pages", {}).values()), None)
    if not qid:
        return None
    claims = requests.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json", headers=_UA,
                          timeout=20).json()["entities"][qid].get("claims", {})

    def value(prop):
        try:
            return claims[prop][0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError):
            return None
    # Wikidata is sometimes wrong (a series with a film id) — try each id in turn
    for prop, kind in (("P4947", "movie"), ("P4983", "series")):
        if value(prop):
            try:
                return details(kind, value(prop))
            except TmdbError:
                pass
    if value("P345"):
        return _from_imdb(value("P345"))
    # no ids on Wikidata: the article title, minus "(фільм)" / "(TV series)"
    name = re.sub(r"\s*\([^)]*\)\s*$", "", requests.utils.unquote(page).replace("_", " "))
    found = identify(name)
    return details(found["kind"], found["tmdb_id"]) if found else None


def from_url(url: str) -> dict | None:
    if m := re.search(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d+)", url):
        return _from_imdb(m.group(1))
    if m := re.search(r"themoviedb\.org/(movie|tv)/(\d+)", url):
        return details("series" if m.group(1) == "tv" else "movie", m.group(2))
    if m := re.search(r"//([a-z\-]+)\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)", url):
        return _from_wikipedia(m.group(1), m.group(2))
    # any other page: its og:title / <title> as a film name
    html = requests.get(url, headers=_UA, timeout=20).text
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html) \
        or re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    if not m:
        return None
    name = re.split(r"\s+[|—–-]\s+", requests.utils.unquote(m.group(1)).strip())[0]
    found = identify(name)
    return details(found["kind"], found["tmdb_id"]) if found else None
