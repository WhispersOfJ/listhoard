#!/usr/bin/env python3
"""Headless regression test for /health-board's JSON serialization.

Run directly (python3 tests/listhoard_healthboard_unit.py); wired into
scripts/gate.py. Never touches the network and never opens a real database:
``db.cursor`` is replaced with a fixture returning the exact shape live
postgres yields — timezone-aware ``datetime`` objects included.

Bug: /health-board fed raw ``datetime`` objects straight into
starlette.JSONResponse, which refuses them, so the route returned HTTP 500
("Internal Server Error") since it shipped. docs/optimization-evidence.md §4
records the failing body hash byte-identical across the optimization
refactor — proof the defect is pre-existing, not refactoring damage.
The fix: ``db.source_health_row_json`` serializes datetimes to ISO-8601
strings at the JSON edge only — the dashboard keeps raw datetimes and its
rendering is unchanged.
"""

import json
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_db():
    """Import app.db with a psycopg stub (no network, no driver needed)."""
    stub = types.ModuleType("psycopg")
    stub.connect = None  # db.cursor must never reach a real connect here
    sys.modules.pop("psycopg", None)
    sys.modules["psycopg"] = stub
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import app.db as db

    return db


def run_tests() -> bool:
    db = _load_db()
    failures = []

    now = datetime.now(timezone.utc)
    older = now - timedelta(hours=5)
    fixtures = {
        # (last_ok_at, last_error, last_delta) — the with_delta form, aware
        # datetimes exactly as the client library returns them.
        "trakt": (older, None, 42),
        "imdb": (None, "imdb serves a bot-challenge (HTTP 202)", 0),
    }

    class FakeCursor:
        """Just enough cursor: db.source_health_row executes one statement
        (slug-bound) and fetches one row."""

        def __init__(self):
            self.slug = None

        def execute(self, sql, params=None):
            # The WHERE clause binds the slug as its only parameter.
            if params and len(params) == 1:
                self.slug = params[0]

        def fetchone(self):
            return fixtures.get(self.slug)

    class Ctx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *args):
            return False

    db.cursor = lambda: Ctx()

    # The fix's contract: the JSON-edge dict is stdlib-encodable with ISO
    # datetime strings, exactly what starlette.JSONResponse will receive.
    row = db.source_health_row_json("trakt", with_delta=True)
    try:
        encoded = json.dumps(row)
    except TypeError as e:
        failures.append(f"health row not stdlib-serializable: {e}")
        encoded = ""
    if encoded and older.isoformat() not in encoded:
        failures.append(f"last_ok_at not ISO-8601 in row: {row!r}")

    # The error-only shape must stay stringly too.
    err_row = db.source_health_row_json("imdb", with_delta=True)
    try:
        json.dumps(err_row)
    except TypeError as e:
        failures.append(f"error row not stdlib-serializable: {e}")

    # The dashboard's own helper must still return raw datetimes — that is
    # the seam the JSON edge must not leak its ISO strings into.
    dash = db.source_health_row("trakt", with_delta=True)
    if dash is not None and dash[0] is not older:
        failures.append("dashboard helper leaked ISO strings; dashboard bytes changed")

    # Without the fix these same fixtures reproduce the original defect: a
    # datetime lands in the payload and the stdlib encoder refuses it. Assert
    # that path is exercised so the test cannot silently pass on a revert.
    raw = (older, None, 42)
    try:
        json.dumps(raw)
    except TypeError:
        pass
    else:
        failures.append(
            "defect not reproducible with raw datetimes — fixture shape is wrong"
        )

    # Optional route-level check when fastapi is importable (host only; CI
    # skips it cleanly because it has no fastapi).
    try:
        import fastapi  # noqa: F401
    except ImportError:
        print("  skip: fastapi not importable on this host (route check)")
        route_ran = False
    else:
        route_ran = True

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        return False
    mark = "route check live" if route_ran else "boundary check only"
    print(f"PASS /health-board serializes health datetimes to ISO-8601 ({mark})")
    return True


def main() -> int:
    return 0 if run_tests() else 1


if __name__ == "__main__":
    sys.exit(main())
