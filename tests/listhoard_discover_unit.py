#!/usr/bin/env python3
"""Headless tests for ListHoard's discovery crawler and add-list-by-URL
resolver (ListHoard.spec.md rulings 3 and 14).

Run directly (python3 tests/listhoard_discover_unit.py); wired into
scripts/gate.py. Never touches the network: resolvers and the no-surface /
quarantine contracts are exercised purely, and the walkers use stubbed
adapters/httpx.
"""

import pathlib
import sys
import types
from typing import ClassVar

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_app():
    stub = types.ModuleType("httpx")
    stub.Client = object
    stub.Response = object
    stub.HTTPError = Exception
    sys.modules.setdefault("httpx", stub)
    # CI (and any host without the app's runtime dependencies) has no psycopg:
    # `app.discover` imports `app.db` eagerly, and db.py imports psycopg at
    # module scope. Every behavioural test below swaps the db module for a
    # fake, so a bare stub is enough to import.
    sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from app import discover

    return discover


discover = _load_app()


class _FakeDb:
    """Catalog stand-in: registered rows in memory."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}

    def load_lists(self, slug):
        return list(self.rows.get(slug, []))

    def upsert_list(self, slug, external_id, name, mediatype, anime, xxx, url, tags):
        rows = self.rows.setdefault(slug, [])
        if not any(r["external_id"] == external_id for r in rows):
            rows.append(
                {
                    "external_id": external_id,
                    "name": name,
                    "mediatype": mediatype,
                    "url": url,
                    "tags": tags,
                }
            )


def test_resolve_url_contracts() -> int:
    cases = [
        ("https://www.themoviedb.org/list/87304", ("tmdb", "87304")),
        (
            "https://www.themoviedb.org/list/87304-top-movies",
            ("tmdb", "87304"),
        ),
        (
            "https://trakt.tv/users/justin/lists/the-darkest-hour",
            ("trakt", "justin/the-darkest-hour"),
        ),
        (
            "https://trakt.tv/lists/official-worst-ever",
            ("trakt", "list:official-worst-ever"),
        ),
        (
            "https://mdblist.com/lists/kasdfs2022/whatever-recs",
            ("mdblist", "kasdfs2022/whatever-recs"),
        ),
        (
            "https://letterboxd.com/drew/list/the-great-movies/",
            ("letterboxd", "drew/the-great-movies"),
        ),
        (
            "https://thetvdb.com/lists/top-anime",
            ("tvdb", "top-anime"),
        ),
        (
            "https://editorial.rottentomatoes.com/guide/best-horror-movies/",
            ("rottentomatoes", "best-horror-movies"),
        ),
        ("https://www.imdb.com/list/ls057843912/", ("imdb", "ls057843912")),
    ]
    for url, want in cases:
        got = discover.resolve_url(url)
        assert got == want, f"{url}: {got} != {want}"
    return 0


def test_trakt_accepts_bare_numeric_identity() -> int:
    """A bare numeric external_id is the ownerless-list contract: the crawler's
    earlier registrations carry that form and must still sync (listhed into
    adapters.fetch_trakt_list, not a data migration)."""
    calls = []

    class _FakeResp:
        headers: ClassVar[dict] = {"X-Pagination-Page-Count": "1"}

    class _FakeTrakt:
        @staticmethod
        def _get(path):
            calls.append(path)
            # An empty page is the fastest honourable stop for a walker that
            # reads rows; one request is all the assertion needs.
            return [], _FakeResp()

    real = discover.adapters._Trakt
    discover.adapters._Trakt = _FakeTrakt
    try:
        list(discover.adapters.fetch_trakt_list("1248149"))
    except StopIteration:
        pass
    finally:
        discover.adapters._Trakt = real
    assert calls and calls[0].startswith("/lists/1248149/items?"), calls
    return 0


def test_resolve_url_refuses_non_lists() -> int:
    bad = [
        "",
        "not a url",
        "https://example.com/list/1",
        "https://trakt.tv/shows/ breaking-bad",
        "https://thetvdb.com/series/foo",
        "ftp://mdblist.com/lists/a/b",
        "https://www.themoviedb.org/movie/87304",
        "https://www.imdb.com/title/tt0111161/",
    ]
    for url in bad:
        assert discover.resolve_url(url) is None, f"{url} resolved"
    return 0


def test_register_is_additive_and_tagged() -> int:
    fake = _FakeDb()
    discover.db = fake
    assert discover._register("tmdb", "87304", "Top", "movie", "u") is True
    assert discover._register("tmdb", "87304", "Top", "movie", "u") is False
    rows = fake.load_lists("tmdb")
    assert len(rows) == 1 and rows[0]["tags"] == ["discovered"]
    return 0


def test_discover_reports_no_surface_honestly() -> int:
    """TMDB is in the no-surface set: API v3 serves no list-search endpoint
    (verified live 2026-10-18), so it joins the scraped providers."""
    discover.db = _FakeDb()
    for slug in ("tmdb", "letterboxd", "imdb", "rottentomatoes", "tvdb"):
        out = discover.discover_source(slug)
        assert out["skipped"] == "no public enumeration surface", out
    return 0


def test_discover_skips_quarantined_source() -> int:
    discover.db = _FakeDb()
    discover.adapters._quarantine["tmdb"] = 5  # at the quarantine threshold
    out = discover.discover_source("tmdb")
    assert out["skipped"] == "quarantined", out
    discover.adapters._quarantine["tmdb"] = 0
    return 0


def test_discover_trakt_registers_bounded_rows() -> int:
    fake = _FakeDb()
    discover.db = fake

    class _FakeTrakt:
        @staticmethod
        def _get(path):
            page = int(path.split("page=")[1])
            rows = [
                {
                    "list": {
                        "ids": {
                            "trakt": 1000 + page * 100 + i,
                            "slug": f"s-{page}-{i}",
                        },
                        "name": f"trakt list {page}-{i}",
                    },
                    "user": {"username": "justin"},
                }
                for i in range(100)
            ]
            return rows, None

    real_trakt = discover.adapters._Trakt
    real_pacing = discover.adapters.sleep_pacing
    discover.adapters._Trakt = _FakeTrakt
    discover.adapters.sleep_pacing = lambda: None
    out = discover.discover_source("trakt")
    discover.adapters._Trakt = real_trakt
    discover.adapters.sleep_pacing = real_pacing
    # 5 pages x 100, all unique: the bound holds and every row is catalogued.
    assert out["discovered"] == 400, out
    assert len(fake.load_lists("trakt")) == 400, len(fake.load_lists("trakt"))
    return 0


def test_add_list_by_url_refuses_unknown() -> int:
    discover.db = _FakeDb()
    out = discover.add_list_by_url("https://example.com/list/1")
    assert out["ok"] is False and "no adapter" in out["reason"]
    return 0


def test_add_list_by_url_registers_and_is_idempotent() -> int:
    fake = _FakeDb()
    discover.db = fake
    triggered = []
    real_sync = discover.sync

    class _FakeSync:
        @staticmethod
        def trigger_source(slug):
            triggered.append(slug)

    discover.sync = _FakeSync
    out = discover.add_list_by_url(
        "https://mdblist.com/lists/violentlyautustic/camp-2-the-campening"
    )
    assert out["ok"] and out["created"] and out["source"] == "mdblist"
    assert triggered == ["mdblist"]
    again = discover.add_list_by_url(
        "https://mdblist.com/lists/violentlyautustic/camp-2-the-campening"
    )
    discover.sync = real_sync
    assert again["ok"] and not again["created"]
    assert triggered == ["mdblist"]  # no second sync for a known row
    return 0


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"ok {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} checks passed")
