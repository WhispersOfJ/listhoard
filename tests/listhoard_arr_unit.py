#!/usr/bin/env python3
"""Headless tests for ListHoard's arr add path.

Run directly (python3 tests/listhoard_arr_unit.py); wired into
scripts/gate.py. Never touches the network: `arr._call` and `arr.lookup`
are replaced with stubs, so only the add/status-code logic runs.

Regression: `add_movie`/`add_series` used to require exactly HTTP 200, but
a successful first-time add answers 201 Created on both arrs (Radarr
MovieController.cs `return Created(movie.Id)`; Sonarr SeriesController.cs
`return Created(series.Id)`), so every fresh add was logged as
`add failed: 201` while the title had in fact been created.
"""

import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_app():
    """Import listhoard's app.arr with a stub httpx in place (same loading
    pattern as tests/listhoard_scrapers_unit.py). Reloaded per test so the
    stubs below cannot leak across tests."""
    stub = types.ModuleType("httpx")
    stub.Client = object
    stub.Response = object
    stub.HTTPError = Exception
    sys.modules.pop("httpx", None)
    sys.modules["httpx"] = stub
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import app.arr as arr_mod

    return arr_mod


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


ITEM = {"tmdb_id": 603, "title": "The Matrix", "title_slug": "the-matrix"}

OPTS = {
    "profile_id": 1,
    "root_folder": "/data/media/movies",
    "monitor": True,
    "minimum_availability": "released",
    "tags": [7],
    "search_on_add": False,
}


def _install(arr_mod, status_code: int, body: dict) -> list[tuple[str, str]]:
    """Stub arr.lookup + arr._call so the POST gets status_code; record the
    posted (method, path) pairs so a test asserts on the real add routes."""
    recorded: list[tuple[str, str]] = []

    def fake_lookup(_app, _item):
        return {"id": 123, "title": ITEM["title"], "tmdbId": ITEM["tmdb_id"]}

    def fake_call(_app, method, path, params=None, json=None, token=None):
        assert (method, path) in (
            ("POST", "/api/v3/movie"),
            ("POST", "/api/v3/series"),
        ), f"{method} {path}"
        recorded.append((method, path))
        return _FakeResponse(status_code, body)

    arr_mod._call = fake_call
    arr_mod.lookup = fake_lookup
    return recorded


def test_add_movie_accepts_201_created():
    arr_mod = _load_app()
    calls = _install(arr_mod, 201, {"id": 42})
    assert arr_mod.add_movie(ITEM, OPTS) == 42
    assert calls == [("POST", "/api/v3/movie")]


def test_add_series_accepts_201_created():
    arr_mod = _load_app()
    calls = _install(arr_mod, 201, {"id": 7, "seasons": []})
    assert arr_mod.add_series(ITEM, OPTS) == 7
    assert calls == [("POST", "/api/v3/series")]


def test_add_movie_still_accepts_200():
    """An add that answers 200 (e.g. the already-exists shape) stays valid."""
    arr_mod = _load_app()
    _install(arr_mod, 200, {"id": 13})
    assert arr_mod.add_movie(ITEM, OPTS) == 13


def test_add_movie_rejects_4xx_and_5xx():
    """Only 2xx counts as success; anything else raises ArrError."""
    arr_mod = _load_app()
    for code in (301, 400, 409, 500):
        _install(arr_mod, code, {})
        try:
            arr_mod.add_movie(ITEM, OPTS)
        except arr_mod.ArrError as e:
            assert str(code) in str(e), e
        else:
            raise AssertionError(f"status {code} was accepted as success")


def test_add_movie_rejects_missing_id_body():
    """A 2xx without an integer `id` is still an error (kept from before)."""
    arr_mod = _load_app()
    _install(arr_mod, 201, {})
    try:
        arr_mod.add_movie(ITEM, OPTS)
    except arr_mod.ArrError as e:
        assert "no id" in str(e), e
    else:
        raise AssertionError("missing-id body was accepted")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(
        (k, v) for k, v in sorted(globals().items()) if k.startswith("test_")
    ):
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001 — the runner reports, not swallows
            fails += 1
            print(f"FAIL {name}: {e}")
    if fails:
        print(f"{fails} test(s) failed", file=sys.stderr)
        sys.exit(1)
    print("all listhoard arr unit tests passed")
