"""Pluggable auth (the shippability specification §7).

One `LISTHOARD_AUTH_MODE` env, three modes:

- `jellyfin` — the current model (default when `JELLYFIN_*` is configured):
  credentials stay in Jellyfin, `arr.authenticate_by_name` decides.
- `local` — one built-in admin account bootstrapped from env
  (`LISTHOARD_LOCAL_ADMIN_USER` / `..._PASSWORD`); any arr-and-postgres user
  can self-host without Jellyfin.
- `token` — one shared admin secret (`LISTHOARD_ADMIN_TOKEN`), compared
  constant-time; the simplest one-person mode.

Sessions live in postgres for every mode (the in-memory set dies with
shippability): restart-safe, garbage-collected by the daily sync pass. The
cookie mechanics are unchanged from the pre-shippability contract (7-day
`max_age`, `httponly`, `lh_session`).

Password hashing uses stdlib `hashlib.scrypt` — a memory-hard KDF, the
stdlib's own recommendation for password material — formatted
`scrypt$<n>$<r>$<p>$<b64salt>$<b64hash>`. This keeps the zero-extra-dependency
packaging law (Shippability §11) while matching bcrypt/argon2-class strength;
where the spec's interview text named argon2, this implementation records the
substitution in its own docstring instead of pulling a binary wheel in.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger("listhoard.auth")

_SESSION_TTL_DAYS = 7  # mirrors the cookie max_age


def resolved_mode() -> str | None:
    """The effective auth mode: env wins, else auto-detect.

    Returns None when nothing is configured — the state the setup flow owns
    (ruling 4: the app never blanks; /login names what is missing).
    """
    mode = (os.environ.get("LISTHOARD_AUTH_MODE") or "").strip().lower()
    if mode:
        if mode not in ("jellyfin", "local", "token"):
            log.error("LISTHOARD_AUTH_MODE=%r is not one of jellyfin|local|token", mode)
            return None
        return mode
    if os.environ.get("JELLYFIN_API_KEY") and os.environ.get("JELLYFIN_BASE_URL"):
        return "jellyfin"
    return None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, n: int = 16384, r: int = 8, p: int = 1) -> str:
    """One scrypt line the `accounts.password_hash` column stores."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify against one stored line; False on any malformation
    (including a missing row — the caller passes None)."""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(digest, _unb64(digest_b64))
    except (ValueError, TypeError):
        return False


def token_matches(submitted: str) -> bool:
    """Token mode: constant-time compare against `LISTHOARD_ADMIN_TOKEN`."""
    expected = os.environ.get("LISTHOARD_ADMIN_TOKEN") or ""
    if not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))
