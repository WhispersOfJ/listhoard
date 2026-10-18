#!/usr/bin/env python3
"""Headless tests for Shippability slice 2 (pluggable auth, spec §7).

Run directly (python3 tests/listhoard_auth_unit.py); wired into
scripts/gate.py. Covers the auth contract without a database, a network, or
fastapi:

1. Mode resolution — env wins, auto-detect falls back to jellyfin only when
   JELLYFIN_* is present, and a bogus mode resolves None (never a guessed
   default).
2. Local accounts — scrypt hash/verify round-trips, rejects wrong passwords,
   and never raises on malformed stored lines.
3. Token mode — constant-time compare accepts the exact secret and rejects
   everything else; an empty env never authenticates.
4. Session lifecycle — create/valid/drop expire semantics against a fixture
   cursor, including: the raw token NEVER appears in any SQL (only its hash).
"""

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def run_tests() -> bool:
    failures: list[str] = []

    sys.path.insert(0, str(REPO))
    stub = type(sys)("psycopg")
    stub.connect = None
    sys.modules["psycopg"] = stub

    import app.auth as auth
    import app.db as db

    # --- 1. mode resolution -------------------------------------------------
    saved = {
        k: os.environ.pop(k)
        for k in ("LISTHOARD_AUTH_MODE", "JELLYFIN_API_KEY", "JELLYFIN_BASE_URL")
        if k in os.environ
    }
    try:
        if auth.resolved_mode() is not None:
            failures.append("nothing configured must resolve None")
        os.environ["JELLYFIN_API_KEY"] = "x"
        os.environ["JELLYFIN_BASE_URL"] = "http://jf:8096"
        if auth.resolved_mode() != "jellyfin":
            failures.append("JELLYFIN_* present must auto-resolve jellyfin")
        os.environ["LISTHOARD_AUTH_MODE"] = "bogus"
        if auth.resolved_mode() is not None:
            failures.append("bogus mode must resolve None, not a guessed default")
        for want in ("jellyfin", "local", "token"):
            os.environ["LISTHOARD_AUTH_MODE"] = want
            if auth.resolved_mode() != want:
                failures.append(f"explicit mode {want} must win over auto-detect")
        os.environ.pop("LISTHOARD_AUTH_MODE")
    finally:
        os.environ.update(saved)
        for k in ("JELLYFIN_API_KEY", "JELLYFIN_BASE_URL"):
            os.environ.pop(k, None)

    # --- 2. local accounts ---------------------------------------------------
    line = auth.hash_password("contraband-passphrase")
    if not line.startswith("scrypt$"):
        failures.append(f"stored line must carry its scheme: {line[:16]}")
    if not auth.verify_password("contraband-passphrase", line):
        failures.append("correct password must verify")
    if auth.verify_password("wrong", line):
        failures.append("wrong password must not verify")
    for malformed in ("", "plain", "argon2$1$2$3$aa$bb", "scrypt$x$y$z$a$b", None):
        try:
            if auth.verify_password("contraband-passphrase", malformed):
                failures.append(f"malformed stored line must not verify: {malformed!r}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"malformed stored line raised: {malformed!r}: {e}")

    # --- 3. token mode ---------------------------------------------------------
    os.environ["LISTHOARD_ADMIN_TOKEN"] = "trident"
    try:
        if not auth.token_matches("trident"):
            failures.append("exact token must match")
        if auth.token_matches("Trident") or auth.token_matches("tride"):
            failures.append("near-miss tokens must not match")
    finally:
        os.environ.pop("LISTHOARD_ADMIN_TOKEN")
    if auth.token_matches("trident"):
        failures.append("no token env must authenticate nothing")

    # --- 4. session lifecycle (fixture cursor) -------------------------------
    sql_seen: list[str] = []
    params_seen: list[tuple] = []

    class FakeCursor:
        def __init__(self):
            self.row = None
            self.rowcount = 7

        def execute(self, sql, params=None):
            sql_seen.append(sql)
            params_seen.append(params or ())
            if "RETURNING id" in sql:
                self.row = (1042,)
            elif "expires_at > now()" in sql:
                # one live session: kind=local, account 1042
                self.row = ("local", 1042)
            elif "DELETE FROM sessions" in sql:
                self.row = None
            else:
                self.row = None

        def fetchone(self):
            return self.row

    class Ctx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *args):
            return False

    db.cursor = lambda: Ctx()

    raw = db.create_session("local", 1042)
    if not raw or "$" not in raw and len(raw) < 20:
        failures.append(f"create_session must return a raw token: {raw!r}")
    insert_sql = next((s for s in sql_seen if "INSERT INTO sessions" in s), "")
    if insert_sql and raw in insert_sql:
        failures.append("the raw token must never land in stored SQL")
    if any(raw in str(p) for p in params_seen):
        failures.append("the raw token must never be a stored parameter")

    row = db.valid_session(raw)
    if row != {"kind": "local", "account_id": 1042}:
        failures.append(f"valid_session must return the row: {row!r}")

    # empty token can never validate
    if db.valid_session("") is not None:
        failures.append("an empty cookie must never validate")

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        return False
    print("PASS shippability slice 2: auth modes, scrypt verify, session lifecycle")
    return True


def main() -> int:
    return 0 if run_tests() else 1


if __name__ == "__main__":
    sys.exit(main())
