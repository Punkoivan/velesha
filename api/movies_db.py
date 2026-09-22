"""Personal watched-movie ratings (ADR-0042).

Lives alongside the rest of our own state in `api/data/` (gitignored, same
place as usage.json and per-user notes) — this agent runs on this machine,
not the HA server, so there is no HA backup to hook into (open question,
left to the user).

Keyed by imdb_id when known, else (title, year) — not jellyfin_id: the user
deletes files from Jellyfin once storage fills up, so the local library id
is not a durable identifier (ADR-0042 update).
"""
import json
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "data" / "movies.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watched_movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id TEXT,
    title TEXT NOT NULL,
    year INTEGER,
    director TEXT,
    genres TEXT,
    rating REAL,
    review TEXT,
    watched_date TEXT DEFAULT (datetime('now')),
    qdrant_indexed INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qdrant_pending ON watched_movies(qdrant_indexed);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _find_existing(conn: sqlite3.Connection, imdb_id: str | None, title: str, year: int | None) -> int | None:
    if imdb_id:
        row = conn.execute("SELECT id FROM watched_movies WHERE imdb_id = ?", (imdb_id,)).fetchone()
        if row:
            return row["id"]
    row = conn.execute("SELECT id FROM watched_movies WHERE lower(title) = lower(?) AND year IS ?", (title, year)).fetchone()
    return row["id"] if row else None


def log_watched_movie(title: str, year: int | None, imdb_id: str | None, director: str | None,
                       genres: list[str], rating: float, review: str | None) -> dict:
    """Insert, or update (and flag for re-indexing) if this film was already rated."""
    with _conn() as conn:
        existing = _find_existing(conn, imdb_id, title, year)
        if existing:
            conn.execute(
                """UPDATE watched_movies SET title=?, year=?, imdb_id=COALESCE(?, imdb_id), director=COALESCE(?, director),
                   genres=?, rating=?, review=?, qdrant_indexed=0, updated_at=datetime('now') WHERE id=?""",
                (title, year, imdb_id, director, json.dumps(genres, ensure_ascii=False), rating, review, existing),
            )
            row_id = existing
        else:
            cur = conn.execute(
                """INSERT INTO watched_movies (imdb_id, title, year, director, genres, rating, review)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (imdb_id, title, year, director, json.dumps(genres, ensure_ascii=False), rating, review),
            )
            row_id = cur.lastrowid
        return dict(conn.execute("SELECT * FROM watched_movies WHERE id = ?", (row_id,)).fetchone())


def get_watched_movies(min_rating: float | None = None, genre: str | None = None, limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM watched_movies ORDER BY watched_date DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["genres"] = json.loads(d["genres"] or "[]")
        if min_rating is not None and (d["rating"] or 0) < min_rating:
            continue
        if genre and genre.lower() not in [g.lower() for g in d["genres"]]:
            continue
        out.append(d)
    return out[:limit]


def mark_indexed(row_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE watched_movies SET qdrant_indexed = 1 WHERE id = ?", (row_id,))
