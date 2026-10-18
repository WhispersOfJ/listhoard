"""ListHoard catalog database: schema and helpers.

The schema matches the ListHoard specification §6.4. The app is the only
writer of the `listhoard_catalog` database; this module owns the DDL and the
connection, both idempotent so container restarts are safe.

Refactor note (the optimization specification §7.4): the repeated query
patterns the UI endpoints used to spell inline now live here as named
helpers — source health rows, list row, prior imports, import row — so
main.py routes shape requests, not SQL.
"""

import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg

log = logging.getLogger("listhoard.db")

# Slow-query threshold (the optimization specification §7.5): the cursor
# wrapper logs any statement above it with its elapsed and a short statement
# shape. No parameters are ever logged — statement shape only.
_SLOW_QUERY_MS = float(os.environ.get("LISTHOARD_SLOW_QUERY_MS", "250"))

# Session TTL (Shippability §7): mirrors the cookie's 7-day max_age.
_SESSION_TTL_DAYS = 7

_LOCK = threading.Lock()


class _SlowQueryCursor:
    """Wraps the real cursor, timing every execute and logging the slow ones.
    The log line carries the statement's first 120 characters — a shape, never
    a parameter value."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=None):
        start = time.perf_counter()
        try:
            if params is None:
                return self._cur.execute(sql)
            return self._cur.execute(sql, params)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if elapsed_ms >= _SLOW_QUERY_MS:
                log.warning(
                    "slow query (%.0f ms): %.120s",
                    elapsed_ms,
                    " ".join(sql.split()),
                )

    def executemany(self, sql, seq):
        start = time.perf_counter()
        try:
            return self._cur.executemany(sql, seq)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if elapsed_ms >= _SLOW_QUERY_MS:
                log.warning(
                    "slow query-batch (%.0f ms, n=%d): %.120s",
                    elapsed_ms,
                    len(seq),
                    " ".join(sql.split()),
                )

    def __getattr__(self, name):
        return getattr(self._cur, name)


_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    id      serial PRIMARY KEY,
    slug    text UNIQUE NOT NULL,
    kind    text NOT NULL,              -- api | scrape | virtual
    enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS lists (
    id              serial PRIMARY KEY,
    source_id       integer REFERENCES sources(id),
    external_id     text NOT NULL,
    name            text NOT NULL,
    mediatype       text NOT NULL,      -- movie | show | mixed
    anime           boolean NOT NULL DEFAULT false,
    xxx             boolean NOT NULL DEFAULT false,
    item_count      integer NOT NULL DEFAULT 0,
    url             text,
    tags            text[] NOT NULL DEFAULT '{}',
    last_synced_at  timestamptz,
    last_error      text,
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS items (
    id            serial PRIMARY KEY,
    mediatype     text NOT NULL,
    tmdb_id       bigint,
    tvdb_id       bigint,
    trakt_id      bigint,
    title         text NOT NULL,
    year          integer,
    xxx           boolean NOT NULL DEFAULT false,
    poster_path   text,
    backdrop_path text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS items_identity
    ON items (mediatype, coalesce(tmdb_id, 0), coalesce(tvdb_id, 0));

CREATE TABLE IF NOT EXISTS list_items (
    list_id  integer REFERENCES lists(id) ON DELETE CASCADE,
    item_id  integer REFERENCES items(id) ON DELETE CASCADE,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, item_id)
);

CREATE TABLE IF NOT EXISTS imports (
    id           serial PRIMARY KEY,
    list_id      integer REFERENCES lists(id),
    app          text NOT NULL,          -- radarr | sonarr
    tag_label    text NOT NULL,
    root_folder  text NOT NULL,
    profile_id   integer NOT NULL,
    series_type  text,
    search_on_add boolean NOT NULL DEFAULT true,
    added_at     timestamptz NOT NULL DEFAULT now(),
    added        integer NOT NULL DEFAULT 0,
    failed       integer NOT NULL DEFAULT 0,
    status       text NOT NULL           -- running | done | failed
);

CREATE TABLE IF NOT EXISTS import_items (
    import_id integer REFERENCES imports(id) ON DELETE CASCADE,
    item_id   integer REFERENCES items(id),
    arr_id    bigint,
    status    text NOT NULL,             -- added | skipped-present | skipped-unresolvable | failed
    detail    text
);

CREATE TABLE IF NOT EXISTS resolutions (
    item_id     integer REFERENCES items(id),
    app         text NOT NULL,
    arr_id      bigint,
    title       text,
    missed      boolean NOT NULL,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, app)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id    integer PRIMARY KEY REFERENCES sources(id),
    last_ok_at   timestamptz,
    last_error   text,
    last_delta   integer
);

-- The UX layer's hoard-owned annotations (ListHoard-UX.spec.md §5.3):
-- per-item note and starred flag. Arr/Jellyfin state is never touched.
ALTER TABLE items ADD COLUMN IF NOT EXISTS note text;
ALTER TABLE items ADD COLUMN IF NOT EXISTS starred boolean NOT NULL DEFAULT false;

-- Shippability slice 2 (the shippability specification §7): sessions live
-- here for every auth mode (the in-memory set dies), keyed by the token's
-- SHA-256 — never the raw token. Accounts back local mode only.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    kind       text NOT NULL,             -- jellyfin | local | token
    account_id integer,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id            serial PRIMARY KEY,
    username      text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    disabled      boolean NOT NULL DEFAULT false
);
"""


def _conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'legends')} "
        f"password={os.environ['POSTGRES_PASSWORD']} "
        f"dbname={os.environ.get('POSTGRES_DB', 'listhoard_catalog')}"
    )


@contextmanager
def cursor():
    with _LOCK, psycopg.connect(_conninfo()) as conn:
        with conn.cursor() as raw:
            yield _SlowQueryCursor(raw)
        conn.commit()


def init_schema() -> None:
    with cursor() as cur:
        cur.execute(_DDL)


def ensure_source(slug: str, kind: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO sources (slug, kind) VALUES (%s, %s) "
            "ON CONFLICT (slug) DO NOTHING",
            (slug, kind),
        )


def source_id(slug: str) -> int:
    with cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"source '{slug}' not registered")
    return int(row[0])


def upsert_list(
    source_slug: str,
    external_id: str,
    name: str,
    mediatype: str,
    anime: bool,
    xxx: bool,
    url: str | None,
    tags: list[str],
) -> None:
    with cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE slug = %s", (source_slug,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"source '{source_slug}' missing")
        cur.execute(
            """
            INSERT INTO lists (source_id, external_id, name, mediatype, anime, xxx, url, tags)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, external_id)
            DO UPDATE SET name = EXCLUDED.name,
                          mediatype = EXCLUDED.mediatype,
                          anime = EXCLUDED.anime,
                          xxx = EXCLUDED.xxx,
                          url = EXCLUDED.url,
                          tags = EXCLUDED.tags
            """,
            (int(row[0]), external_id, name, mediatype, anime, xxx, url, tags),
        )


def load_lists(source_slug: str) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.name, l.external_id, l.item_count, l.xxx,
                   l.last_synced_at, l.last_error
            FROM lists l JOIN sources s ON s.id = l.source_id
            WHERE s.slug = %s
            ORDER BY l.name
            """,
            (source_slug,),
        )
        cols = [
            "id",
            "name",
            "external_id",
            "item_count",
            "xxx",
            "last_synced_at",
            "last_error",
        ]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def update_list_sync(
    list_id: int, item_count: int | None = None, error: str | None = None
) -> None:
    with cursor() as cur:
        if error is None:
            cur.execute(
                "UPDATE lists SET item_count = %s, last_synced_at = now(),"
                " last_error = NULL WHERE id = %s",
                (item_count, list_id),
            )
        elif item_count is None:
            cur.execute(
                "UPDATE lists SET last_error = %s WHERE id = %s",
                (error, list_id),
            )
        else:
            cur.execute(
                "UPDATE lists SET last_error = %s WHERE id = %s",
                (error, list_id),
            )


def _iso(value: object) -> object:
    """JSON-safe form of one row value: datetimes become ISO-8601 strings.
    starlette.JSONResponse refuses datetime objects, which is how
    /health-board used to 500 (docs/optimization-evidence.md §4)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def health_update(source_slug: str, error: str | None, delta: int) -> None:
    with cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE slug = %s", (source_slug,))
        row = cur.fetchone()
        if row is None:
            return
        if error is None:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_ok_at, last_error, last_delta)
                VALUES (%s, now(), NULL, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    last_ok_at = now(), last_error = NULL, last_delta = EXCLUDED.last_delta
                """,
                (int(row[0]), delta),
            )
        else:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_error, last_delta)
                VALUES (%s, %s, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    last_error = EXCLUDED.last_error
                """,
                (int(row[0]), error, delta),
            )


def source_health_row(
    slug: str, with_delta: bool = False
) -> tuple[object, object, object]:
    """One source's health row as a bare tuple — what the dashboard and the
    health-board each unpack. Values stay raw (``datetime`` objects included)
    so both consumers see the same row; the JSON edge applies ``_iso``.
    `None` when no health row exists."""
    cols = "sh.last_ok_at, sh.last_error"
    if with_delta:
        cols = "sh.last_ok_at, sh.last_error, sh.last_delta"
    with cursor() as cur:
        cur.execute(
            f"SELECT {cols}"
            " FROM source_health sh JOIN sources s ON s.id = sh.source_id"
            " WHERE s.slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    return row


def source_health_row_json(
    slug: str, with_delta: bool = False
) -> dict[str, object] | None:
    """The health row JSONResponse can encode: datetimes ISO-8601 strings.
    This is the JSON edge of source_health_row — the dashboard keeps raw
    datetimes (it renders them as text), while starlette.JSONResponse refuses
    datetime objects, which is how /health-board returned HTTP 500 since it
    shipped (docs/optimization-evidence.md §4)."""
    row = source_health_row(slug, with_delta=with_delta)
    if row is None:
        return None
    keys = ["last_ok_at", "last_error"]
    if with_delta:
        keys.append("last_delta")
    return dict(zip(keys, (_iso(v) for v in row)))


def list_row(list_id: int) -> dict | None:
    """The lists row joined to its source slug — what list_detail renders."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.name, s.slug, l.external_id, l.mediatype, l.anime,
                   l.xxx, l.item_count, l.url,
                   l.last_synced_at, l.last_error
            FROM lists l JOIN sources s ON s.id = l.source_id
            WHERE l.id = %s
            """,
            (list_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = [
        "id",
        "name",
        "source",
        "external_id",
        "mediatype",
        "anime",
        "xxx",
        "item_count",
        "url",
        "last_synced_at",
        "last_error",
    ]
    return dict(zip(cols, row))


def list_brief(list_id: int) -> dict | None:
    """The narrow lists row the import POST reads: name, slug, ids, flags."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT l.name, s.slug, l.external_id, l.mediatype, l.anime, l.xxx
            FROM lists l JOIN sources s ON s.id = l.source_id WHERE l.id = %s
            """,
            (list_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(["name", "slug", "external_id", "mediatype", "anime", "xxx"], row))


def prior_imports(list_id: int) -> list[dict]:
    """One list's import history, newest first."""
    with cursor() as cur:
        cur.execute(
            """SELECT i.id, i.tag_label, i.added, i.failed, i.status
                FROM imports i WHERE i.list_id = %s ORDER BY i.added_at DESC""",
            (list_id,),
        )
        cols = ["import_id", "tag_label", "added", "failed", "status"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def import_row(import_id: int) -> dict | None:
    """One import's row joined to the list's name (what the import page renders)."""
    with cursor() as cur:
        cur.execute(
            """SELECT i.id, l.name, i.app, i.tag_label, i.root_folder, i.status,
                      i.added, i.failed
                FROM imports i LEFT JOIN lists l ON l.id = i.list_id
                WHERE i.id = %s""",
            (import_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = [
        "import_id",
        "list",
        "app",
        "tag_label",
        "root_folder",
        "status",
        "added",
        "failed",
    ]
    return dict(zip(cols, row))


def import_buckets(import_id: int) -> dict:
    """The import_items status bucket counts for one import."""
    with cursor() as cur:
        cur.execute(
            """SELECT ii.status, count(*) FROM import_items ii
                WHERE ii.import_id = %s GROUP BY ii.status""",
            (import_id,),
        )
        return {status: int(n) for status, n in cur.fetchall()}


def import_simple(import_id: int) -> dict | None:
    """The narrow import row confirm_unimport needs: app + tag + status."""
    with cursor() as cur:
        cur.execute(
            "SELECT app, tag_label, status FROM imports WHERE id = %s", (import_id,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(["app", "tag_label", "status"], row))


def resolve_item(src: dict) -> int:
    """Idempotent upsert of one fetched item; returns the catalog item id."""
    return resolve_items_batch([src])[0]


_UPSERT_ITEM_SQL = """
INSERT INTO items (mediatype, tmdb_id, tvdb_id, trakt_id, title, year, xxx,
                   poster_path, backdrop_path)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (mediatype, coalesce(tmdb_id, 0), coalesce(tvdb_id, 0))
DO UPDATE SET title = EXCLUDED.title, year = EXCLUDED.year,
              xxx = EXCLUDED.xxx,
              poster_path = EXCLUDED.poster_path,
              backdrop_path = EXCLUDED.backdrop_path
RETURNING id
"""


def resolve_items_batch(items: list[dict]) -> list[int]:
    """Idempotent upsert of a whole fetched list under one connection and one
    transaction. resolve_item pays a fresh TCP connect per row — invisible on
    a 100-item list, hours on a 40,000-item export."""
    out: list[int] = []
    with _LOCK, psycopg.connect(_conninfo()) as conn:
        with conn.transaction(), conn.cursor() as cur:
            for src in items:
                cur.execute(
                    _UPSERT_ITEM_SQL,
                    (
                        src["mediatype"],
                        src.get("tmdb_id"),
                        src.get("tvdb_id"),
                        src.get("trakt_id"),
                        src["title"],
                        src.get("year"),
                        src.get("xxx", False),
                        src.get("poster_path"),
                        src.get("backdrop_path"),
                    ),
                )
                out.append(int(cur.fetchone()[0]))
        conn.commit()
    return out


def link_items(list_id: int, item_ids: list[int]) -> None:
    with cursor() as cur:
        cur.executemany(
            "INSERT INTO list_items (list_id, item_id) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            [(list_id, i) for i in item_ids],
        )


def list_page(
    offset: int,
    limit: int,
    source: str | None = None,
    mediatype: str | None = None,
    anime: bool | None = None,
    show_xxx: bool = False,
    q: str | None = None,
) -> tuple[dict, int]:
    """Filtered, ordered, paginated browse view for the UI list rows."""
    where, params = [], []
    if source:
        where.append("s.slug = %s")
        params.append(source)
    if mediatype in ("movie", "show"):
        where.append("l.mediatype IN (%s, 'mixed')")
        params.append(mediatype)
    if anime:
        where.append("l.anime")
    if not show_xxx:
        where.append("NOT l.xxx")
    if q:
        where.append("l.name ILIKE %s")
        params.append(f"%{q}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    join = "FROM lists l JOIN sources s ON s.id = l.source_id"
    with cursor() as cur:
        cur.execute(f"SELECT count(*) {join}{w}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""SELECT l.id, l.name, s.slug AS source, l.mediatype, l.anime, l.xxx,
                       l.item_count, l.last_synced_at, l.last_error
                {join}{w}
                ORDER BY s.slug, l.item_count DESC, l.name
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        cols = [
            "id",
            "name",
            "source",
            "mediatype",
            "anime",
            "xxx",
            "item_count",
            "last_synced",
            "last_error",
        ]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows, total


def list_items(list_id: int, offset: int, limit: int) -> tuple[list[dict], int]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM list_items WHERE list_id = %s
            """,
            (list_id,),
        )
        total = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT i.id, i.title, i.year, i.tmdb_id, i.tvdb_id, i.xxx,
                   i.poster_path, i.backdrop_path, i.starred, i.note
            FROM list_items li JOIN items i ON i.id = li.item_id
            WHERE li.list_id = %s
            ORDER BY i.title
            LIMIT %s OFFSET %s
            """,
            (list_id, limit, offset),
        )
        cols = [
            "id",
            "title",
            "year",
            "tmdb_id",
            "tvdb_id",
            "xxx",
            "poster_path",
            "backdrop_path",
            "starred",
            "note",
        ]
        return [dict(zip(cols, r)) for r in cur.fetchall()], total


def catalog_totals() -> dict:
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM items")
        items = int(cur.fetchone()[0])
        cur.execute("""
            SELECT s.slug, count(l.id) FROM sources s
            LEFT JOIN lists l ON l.source_id = s.id
            GROUP BY s.slug ORDER BY s.slug
        """)
        per_source = {slug: int(n) for slug, n in cur.fetchall()}
        cur.execute("SELECT count(*) FROM lists")
        lists = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM lists WHERE last_synced_at IS NOT NULL")
        synced = int(cur.fetchone()[0])
    return {"items": items, "lists": lists, "synced": synced, "per_source": per_source}


def recent_imports(limit: int = 8) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT i.id, l.name, i.app, i.tag_label, i.added, i.failed, i.status
            FROM imports i LEFT JOIN lists l ON l.id = i.list_id
            ORDER BY i.added_at DESC LIMIT %s
            """,
            (limit,),
        )
        cols = ["import_id", "list", "app", "tag_label", "added", "failed", "status"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def set_star(item_id: int, starred: bool) -> None:
    with cursor() as cur:
        cur.execute("UPDATE items SET starred = %s WHERE id = %s", (starred, item_id))


def set_note(item_id: int, note: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE items SET note = %s WHERE id = %s",
            (note.strip() or None, item_id),
        )


def item_detail(item_id: int, list_id: int) -> dict | None:
    """Everything the item drawer shows: the row, its id resolvability, and
    every other catalogued list that holds it."""
    with cursor() as cur:
        cur.execute(
            """SELECT id, mediatype, tmdb_id, tvdb_id, trakt_id, title, year,
                      xxx, starred, note, poster_path, backdrop_path
                FROM items WHERE id = %s""",
            (item_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "mediatype",
            "tmdb_id",
            "tvdb_id",
            "trakt_id",
            "title",
            "year",
            "xxx",
            "starred",
            "note",
            "poster_path",
            "backdrop_path",
        ]
        out = dict(zip(cols, row))
        cur.execute(
            """SELECT l.id, l.name, s.slug FROM list_items li
                JOIN lists l ON l.id = li.list_id
                JOIN sources s ON s.id = l.source_id
                WHERE li.item_id = %s AND l.id != %s
                ORDER BY l.name""",
            (item_id, list_id),
        )
        out["also_in"] = [
            {"id": r[0], "name": r[1], "source": r[2]} for r in cur.fetchall()
        ]
        return out


def failed_rows(import_id: int) -> list[int]:
    """Catalog item ids whose arr add failed inside one import — the set a
    re-issue retries."""
    with cursor() as cur:
        cur.execute(
            """SELECT item_id FROM import_items
                WHERE import_id = %s AND status = 'failed' AND item_id IS NOT NULL""",
            (import_id,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def create_session(kind: str, account_id: int | None) -> str:
    """Mint one session row and return the RAW cookie token (the row stores
    only its SHA-256 — a database leak never yields a usable cookie)."""
    import hashlib as _hashlib
    import secrets as _secrets

    raw = _secrets.token_urlsafe(24)
    token_hash = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO sessions (token_hash, kind, account_id, expires_at)
               VALUES (%s, %s, %s, now() + %s * interval '1 day')""",
            (token_hash, kind, account_id, _SESSION_TTL_DAYS),
        )
    return raw


def valid_session(raw_token: str) -> dict | None:
    """The session row when the raw token is live, else None. Expired rows
    are deleted on sight."""
    import hashlib as _hashlib

    if not raw_token:
        return None
    token_hash = _hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with cursor() as cur:
        cur.execute(
            """SELECT kind, account_id FROM sessions
               WHERE token_hash = %s AND expires_at > now()""",
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "DELETE FROM sessions WHERE token_hash = %s AND expires_at <= now()",
                (token_hash,),
            )
            return None
        return {
            "kind": row[0],
            "account_id": int(row[1]) if row[1] is not None else None,
        }


def drop_session(raw_token: str) -> None:
    """Logout: remove one session row by raw token."""
    import hashlib as _hashlib

    if not raw_token:
        return
    token_hash = _hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


def purge_sessions() -> int:
    """Daily sync's garbage collector (Shippability §7); returns rows killed."""
    with cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
        return cur.rowcount


def upsert_account(username: str, password_hash: str) -> int:
    """Local mode's admin bootstrap: create-or-refresh one account row."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO accounts (username, password_hash)
               VALUES (%s, %s)
               ON CONFLICT (username) DO UPDATE SET
                   password_hash = EXCLUDED.password_hash, disabled = false
               RETURNING id""",
            (username, password_hash),
        )
        return int(cur.fetchone()[0])


def local_account(username: str) -> dict | None:
    """One enabled local account row for authenticate_local's verify step."""
    with cursor() as cur:
        cur.execute(
            """SELECT id, password_hash FROM accounts
               WHERE username = %s AND NOT disabled""",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": int(row[0]), "password_hash": row[1]}


def import_options(import_id: int) -> dict | None:
    """The stored options of one import, exactly what start_import needs."""
    with cursor() as cur:
        cur.execute(
            """SELECT list_id, app, tag_label, root_folder, profile_id,
                      series_type, search_on_add
                FROM imports WHERE id = %s""",
            (import_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    (
        list_id,
        app_name,
        tag_label,
        root_folder,
        profile_id,
        series_type,
        search_on_add,
    ) = row
    return {
        "list_id": int(list_id),
        "app": app_name,
        "tag_label": tag_label,
        "root_folder": root_folder,
        "profile_id": int(profile_id),
        "series_type": series_type,
        "search_on_add": bool(search_on_add),
        "monitor": True,
        "minimum_availability": "released",
        "force": False,
    }


def import_progress(import_id: int) -> dict | None:
    with cursor() as cur:
        cur.execute(
            """SELECT i.app, i.status, i.added, i.failed, i.tag_label, l.name
                FROM imports i LEFT JOIN lists l ON l.id = i.list_id
                WHERE i.id = %s""",
            (import_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            """SELECT ii.status, count(*) FROM import_items ii
                WHERE ii.import_id = %s GROUP BY ii.status""",
            (import_id,),
        )
        buckets = {k: int(v) for k, v in cur.fetchall()}
        cur.execute(
            """SELECT count(DISTINCT item_id) FROM import_items WHERE import_id = %s""",
            (import_id,),
        )
        total = int(cur.fetchone()[0])
    app_name, status, added, failed, tag, list_name = row
    return {
        "import_id": import_id,
        "app": app_name,
        "status": status,
        "added": int(added),
        "failed": int(failed),
        "tag": tag,
        "list": list_name,
        "total": total,
        "buckets": buckets,
    }
