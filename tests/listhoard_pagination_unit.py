#!/usr/bin/env python3
"""Headless tests for multi-page provider walks.

Run directly (python3 tests/listhoard_pagination_unit.py); wired into
scripts/gate.py. Never touches the network: the provider fetchers' HTTP
(entry points) are replaced with fixture sequences so only the pagination
logic runs.

Regression: the Trakt adapter issued a single unpaginated GET (Trakt serves
100 rows per call) and the Letterboxd scraper read only a list's first page,
truncating every list longer than one page to its head rows — the source of
the catalog's 90-99 item bands.
"""

import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_app():
    """Import listhoard's app package with a stub httpx (same pattern as
    tests/listhoard_scrapers_unit.py). The app modules are re-imported per
    test so module-level patches never leak between them."""
    stub = types.ModuleType("httpx")
    stub.Client = object
    stub.Response = object
    stub.HTTPError = Exception
    sys.modules.pop("httpx", None)
    sys.modules["httpx"] = stub
    for name in ("app", "app.adapters", "app.scrapers", "app.ids"):
        sys.modules.pop(name, None)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import app.adapters as ad
    import app.scrapers as sc

    return ad, sc


class _FakeRaw:
    """A httpx.Response stand-in that carries the fields the real client's
    retry loop reads: status_code, headers.get, raise_for_status, json."""

    def __init__(self, status_code: int, body, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def get(self, name: str, default=None):
        return self.headers.get(name, default)

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    """A httpx.Client stand-in replaying a scripted sequence of responses
    through successive get() calls."""

    def __init__(self, **_kw):
        self._calls = 0

    def get(self, url: str, headers=None):
        self._calls += 1
        return _SCRIPTED[min(self._calls, len(_SCRIPTED)) - 1]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_SCRIPTED: list[_FakeRaw] = []


class _FakeResp:
    def __init__(self, page_count: str | None = None):
        self.headers = (
            {} if page_count is None else {"X-Pagination-Page-Count": str(page_count)}
        )


def _trakt_row(mtype: str, n: int) -> dict:
    return {
        "type": mtype,
        mtype: {
            "title": f"{mtype.title()} {n}",
            "year": 2000 + n,
            "ids": {"tmdb": n, "trakt": n, "tvdb": None},
        },
    }


# A 250-row list: pages 1 and 2 carry 100 rows each, page 3 is short (50).
_TRAKT_PAGES = {
    1: (
        [_trakt_row("movie", i) for i in range(1, 101)]
        + [_trakt_row("show", i) for i in range(101, 151)],
        "3",
    ),
    2: ([_trakt_row("movie", i) for i in range(151, 251)], "3"),
    3: ([_trakt_row("show", i) for i in range(251, 301)], "3"),
}


def _db_page(*films) -> str:
    """A Letterboxd list page whose rows carry name + slug attributes."""
    return "\n".join(
        f'<div data-item-name="{t} ({y})" data-details-endpoint="/film/{slug}/json/"></div>'
        for slug, t, y in films
    )


_LB_PAGES = {
    "https://letterboxd.com/u1/list/l1/": _db_page(
        ("f1", "Film One", 2001), ("f2", "Film Two", 2002)
    ),
    "https://letterboxd.com/u1/list/l1/page/2/": _db_page(("f3", "Film Three", 2003)),
}


def test_trakt_walks_every_page() -> int:
    ad, _ = _load_app()
    called: list[str] = []

    def fake_get(self, path: str):
        page = int(path.split("page=")[1])
        called.append(page)
        rows, page_count = _TRAKT_PAGES[page]
        return rows, _FakeResp(page_count)

    ad._Trakt._get = fake_get
    payloads = list(ad.fetch_trakt_list("canconfirm/fake-list"))
    if sorted(called) != [1, 2, 3]:
        print(f"FAIL trakt pages walked: {called}", file=sys.stderr)
        return 1
    if len(payloads) != 300:
        print(f"FAIL trakt payload count: {len(payloads)}", file=sys.stderr)
        return 1
    print("PASS trakt walks every page")
    return 0


def test_trakt_stops_on_short_page_and_headerless() -> int:
    ad, _ = _load_app()
    called: list[str] = []

    def fake_get(self, path: str):
        page = int(path.split("page=")[1])
        called.append(page)
        # No pagination headers at all: full 100-row pages must carry the
        # walk forward and the first empty page must stop it.
        rows = [_trakt_row("movie", i) for i in range(100)] if page <= 2 else []
        return rows, _FakeResp()

    ad._Trakt._get = fake_get
    payloads = list(ad.fetch_trakt_list("x/y"))
    if sorted(called) != [1, 2, 3]:
        print(f"FAIL trakt headerless stop: {called}", file=sys.stderr)
        return 1
    if len(payloads) != 200:
        print(f"FAIL trakt headerless payload count: {len(payloads)}", file=sys.stderr)
        return 1
    print("PASS trakt stops on short or empty pages")
    return 0


def test_trakt_rejects_unexpected_shape() -> int:
    ad, _ = _load_app()

    def fake_get(self, path: str):
        return {"unexpected": True}, _FakeResp()

    ad._Trakt._get = fake_get
    try:
        list(ad.fetch_trakt_list("x/y"))
    except ad.SourceError:
        print("PASS trakt rejects unexpected shape")
        return 0
    print("FAIL trakt shape must raise", file=sys.stderr)
    return 1


def test_letterboxd_walks_every_page() -> int:
    _, sc = _load_app()
    sc.ids.resolve_tmdb_ids = lambda m, t, y: {
        "tmdb_id": 9000,
        "tvdb_id": None,
        "year": y,
        "poster_path": None,
        "backdrop_path": None,
    }

    def pages(url: str, slug: str = "letterboxd") -> str:
        if url not in _LB_PAGES:
            raise sc.adapters.SourceError(f"{url}: 404")
        return _LB_PAGES[url]

    sc._get = pages
    payloads = sc.fetch_letterboxd_list("u1/l1")
    if len(payloads) != 3:
        print(f"FAIL letterboxd payload count: {len(payloads)}", file=sys.stderr)
        return 1
    if [p["title"] for p in payloads] != ["Film One", "Film Two", "Film Three"]:
        print("FAIL letterboxd order", file=sys.stderr)
        return 1
    print("PASS letterboxd walks every page")
    return 0


def test_letterboxd_keeps_partials_on_dead_continuation() -> int:
    """A 404-or-challenge continuation keeps page 1's rows; it must not
    abort the whole sync."""
    _, sc = _load_app()
    sc.ids.resolve_tmdb_ids = lambda m, t, y: {
        "tmdb_id": 9000,
        "tvdb_id": None,
        "year": y,
        "poster_path": None,
        "backdrop_path": None,
    }

    def dead(url: str, slug: str = "letterboxd") -> str:
        if "page/2" in url:
            raise sc.adapters.SourceError(f"{url}: 404")
        return _LB_PAGES["https://letterboxd.com/u1/list/l1/"]

    sc._get = dead
    payloads = sc.fetch_letterboxd_list("u1/l1")
    if [p["title"] for p in payloads] != ["Film One", "Film Two"]:
        print("FAIL letterboxd partial retention", file=sys.stderr)
        return 1
    print("PASS letterboxd keeps partials on dead continuations")
    return 0


def test_trakt_429_backoff_retries() -> int:
    """A rate-limited page is retried on the producer's own clock, not
    dropped: the walk must survive 429s spaced by Retry-After."""
    ad, _ = _load_app()
    ad._trakt_header = lambda: {"trakt-api-key": "test"}
    sleeps: list[float] = []
    ad.time.sleep = sleeps.append
    _SCRIPTED[:] = [
        _FakeRaw(429, [], {"Retry-After": "3"}),
        _FakeRaw(429, [], {"Retry-After": "5"}),
        _FakeRaw(200, [], {"X-Pagination-Page-Count": "1"}),
    ]
    ad.httpx.Client = _FakeClient
    try:
        payloads = list(ad.fetch_trakt_list("x/y"))
    finally:
        _SCRIPTED[:] = []
    if payloads != []:
        print(f"FAIL trakt 429 payload: {payloads}", file=sys.stderr)
        return 1
    if sleeps != [3.0, 5.0]:
        print(f"FAIL trakt 429 backoff sleeps: {sleeps}", file=sys.stderr)
        return 1
    print("PASS trakt backs off and retries on 429")
    return 0


def test_trakt_429_surrenders_after_backoff_exhaustion() -> int:
    """Eight spaced retries, then an honest failure."""
    ad, _ = _load_app()
    ad._trakt_header = lambda: {"trakt-api-key": "test"}
    ad.time.sleep = lambda s: None
    _SCRIPTED[:] = [_FakeRaw(429, [], {})] * 12  # permanent limiter
    ad.httpx.Client = _FakeClient
    try:
        try:
            list(ad.fetch_trakt_list("x/y"))
        except ad.SourceError as e:
            if "rate-limit" not in str(e):
                print(f"FAIL trakt surrender text: {e}", file=sys.stderr)
                return 1
        else:
            print("FAIL trakt must raise on persistent 429", file=sys.stderr)
            return 1
        print("PASS trakt surrenders honestly after backoff exhaustion")
        return 0
    finally:
        _SCRIPTED[:] = []


def test_letterboxd_first_page_failure_raises() -> int:
    """A failure on the FIRST page is a real fetch error: it must raise."""
    _, sc = _load_app()

    def boom(url: str, slug: str = "letterboxd") -> str:
        raise sc.adapters.SourceError(f"{url}: 403")

    sc._get = boom
    try:
        sc.fetch_letterboxd_list("u1/l1")
    except sc.adapters.SourceError:
        print("PASS letterboxd first page failure raises")
        return 0
    print("FAIL letterboxd first-page failure must raise", file=sys.stderr)
    return 1


def main() -> int:
    failures = 0
    for name in sorted(k for k in globals() if k.startswith("test_")):
        if globals()[name]() != 0:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
