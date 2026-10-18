"""SHP-4a: pytest + TestClient API integration (the shippability specification §9).

The repo's first pytest suite. The app is booted for real — FastAPI's
TestClient drives the actual ASGI app — with the database veneered behind
fixture rows (db's psycopg import is stubbed the same way the headless
suites do it), and arr's HTTP surface replaced, so nothing leaves the
process.

Wired into scripts/gate.py behind a capability probe (skips cleanly with a
message when pytest/fastapi are absent — headless-CI-safe like dep-audit.py).
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session", autouse=True)
def _app_cwd():
    """The app resolves `templates/` and `static/` relative to the process
    CWD (the container's WORKDIR is /app). Tests chdir once, session-wide.
    """
    old = os.getcwd()
    os.chdir(REPO)
    yield
    os.chdir(old)


def _load_app(monkeypatch):
    """Boot the real app with psycopg stubbed and SSR-vulnerable seams faked."""
    stub = type(sys)("psycopg")
    stub.connect = None

    monkeypatch.setitem(sys.modules, "psycopg", stub)
    sys.path.insert(0, str(REPO))
    import app.arr as _arr  # noqa: E402
    import app.db as _db  # noqa: E402

    # DATABASE seams: rows the routes will ask for (fixture-catalog shape).
    _db.init_schema = lambda: None
    monkeypatch.setattr(
        _db,
        "source_health_row_json",
        lambda slug, with_delta=False: (
            {"last_ok_at": None, "last_error": None}
            + ({"last_delta": 0} if with_delta else {})
        ),
    )
    # Route seams the dashboard renders: never reaches a real connect.
    monkeypatch.setattr(_db, "recent_imports", lambda limit=8: [])
    monkeypatch.setattr(_db, "source_health_row", lambda slug, with_delta=False: None)
    monkeypatch.setattr(_db, "set_star", lambda item_id, starred: None)
    monkeypatch.setattr(_db, "set_note", lambda item_id, note: None)
    monkeypatch.setattr(_db, "upsert_account", lambda username, ph: 1042)
    monkeypatch.setattr(_db, "local_account", lambda username: None)
    monkeypatch.setattr(_db, "purge_sessions", lambda: 0)
    monkeypatch.setattr(
        _db,
        "catalog_totals",
        lambda: {
            "items": 7,
            "lists": 3,
            "synced": 2,
            "per_source": {"tmdb": 1, "trakt": 2},
        },
    )
    monkeypatch.setattr(
        _arr,
        "authenticate_by_name",
        lambda user, pw: (_ for _ in ()).throw(RuntimeError("no jellyfin")),
    )
    monkeypatch.setattr(_arr, "library", lambda app: {})
    monkeypatch.setattr(_arr, "profiles", lambda app: [])
    monkeypatch.setattr(_arr, "root_folders", lambda app: [])

    from fastapi.testclient import TestClient  # noqa: E402
    import app.main as main  # noqa: E402

    return _db, main, TestClient(main.app)


def _stub_db_module_auth(db, valid: bool):
    """valid_session/upsert/etc. stand-ins the login ride through."""
    db.create_session = lambda kind, account_id: "stub-session-token"
    db.valid_session = lambda raw: (
        {"kind": "token", "account_id": None}
        if raw == "stub-session-token" and valid
        else None
    )
    db.drop_session = lambda raw: None


def test_healthz_reports_counters(monkeypatch):
    db, main, client = _load_app(monkeypatch)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["catalog_items"] == 7
    assert body["lists_synced"] == 2


def test_login_token_mode_end_to_end(monkeypatch):
    db, main, client = _load_app(monkeypatch)
    _stub_db_module_auth(db, valid=True)
    monkeypatch.setenv("LISTHOARD_AUTH_MODE", "token")
    monkeypatch.setenv("LISTHOARD_ADMIN_TOKEN", "correct token")
    try:
        # wrong token → back to the login page with err=1
        r = client.post(
            "/login",
            data={"user": "anyone", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login?err=1")
        # right token → root, with the hoard's session cookie
        r = client.post(
            "/login",
            data={"user": "anyone", "password": "correct token"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        assert "lh_session" in r.headers["set-cookie"]
    finally:
        os.environ.pop("LISTHOARD_AUTH_MODE")
        os.environ.pop("LISTHOARD_ADMIN_TOKEN")


def test_bad_cookie_is_redirected_to_login(monkeypatch):
    db, main, client = _load_app(monkeypatch)
    _stub_db_module_auth(db, valid=False)
    # follow_redirects=False: assert the gate's own 302, not the login page
    # the TestClient would follow it to.
    r = client.get(
        "/jobs/poll", cookies={"lh_session": "bogus"}, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].endswith("/login")


def test_browse_first_run_banner_when_catalog_empty(monkeypatch):
    db, main, client = _load_app(monkeypatch)
    _stub_db_module_auth(db, valid=True)
    # Empty catalog: the first-run funnel (Shippability ruling 8)
    monkeypatch.setattr(
        main.db,
        "catalog_totals",
        lambda: {"items": 0, "lists": 0, "synced": 0, "per_source": {}},
    )
    r = client.get("/", cookies={"lh_session": "stub-session-token"})
    assert r.status_code == 200
    assert "first-run" in r.text
    # The populated catalog never shows the funnel.
    monkeypatch.setattr(
        main.db,
        "catalog_totals",
        lambda: {"items": 7, "lists": 3, "synced": 2, "per_source": {"tmdb": 1}},
    )
    r = client.get("/", cookies={"lh_session": "stub-session-token"})
    assert r.status_code == 200
    assert "first-run" not in r.text


def test_unconfigured_auth_names_its_error(monkeypatch):
    db, main, client = _load_app(monkeypatch)
    _stub_db_module_auth(db, valid=True)
    os.environ["LISTHOARD_AUTH_MODE"] = "bogus"
    try:
        r = client.post(
            "/login", data={"user": "x", "password": "y"}, follow_redirects=False
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login?err=2")
    finally:
        os.environ.pop("LISTHOARD_AUTH_MODE")


def test_list_page_degrades_without_arrs(monkeypatch):
    """The slice-2 degrade-honestly contract on the arr surface: an arr-less
    deployment renders the list page (200) with the wiring notice instead of
    the import form — never a 500.
    """
    db, main, client = _load_app(monkeypatch)
    _stub_db_module_auth(db, valid=True)
    import app.db as _db  # noqa: E402
    import app.importer as _importer  # noqa: E402

    _db.list_row = lambda list_id: {
        "id": list_id,
        "name": "Best of the 21st Century",
        "mediatype": "movie",
        "anime": False,
        "item_count": 3,
        "url": "https://example.com/list",
        "last_synced_at": "now",
        "last_error": None,
    }
    _db.list_items = lambda list_id, offset, limit: ([], 0)
    monkeypatch.setattr(_db, "prior_imports", lambda list_id: [])
    monkeypatch.setattr(_importer, "running_for_list", lambda list_id: None)

    for name in (
        "RADARR_BASE_URL",
        "RADARR_API_KEY",
        "SONARR_BASE_URL",
        "SONARR_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    r = client.get("/lists/1", cookies={"lh_session": "stub-session-token"})
    assert r.status_code == 200, f"arr absence must degrade, not break: {r.status_code}"
    body = r.text
    assert "isn't wired" in body, "the wiring notice must explain the state"
    assert 'id="importer"' not in body, "the import form must not render without an arr"
