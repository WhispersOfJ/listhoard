"""Discovery crawler and add-list-by-URL (ListHoard.spec.md rulings 3 and 14).

Two surfaces, both landing in the catalog without ever importing:

- ``resolve_url`` maps a pasted provider list page URL to ``(source,
  external_id)`` — the adapter's own identity, never a guess.
- ``discover_source`` walks a provider's public enumeration surface
  (TMDB list search, Trakt trending lists, MDBList user directory) and
  registers what it finds under a ``discovered`` tag. Discovered lists join
  the catalog like seed rows: the daily and manual syncs refresh them, and
  import still requires explicit curation (ruling 19).

Pacing floors and the quarantine counters come from ``adapters``; the walk
is hard-bounded so a pager that will not end cannot run away.
"""

import re
import threading
import time

import httpx

from . import adapters, db, sync

# --- URL resolution ---------------------------------------------------------

RESOLVERS: list[tuple[str, re.Pattern[str]]] = [
    ("tmdb", re.compile(r"themoviedb\.org/list/(\d+)")),
    ("trakt", re.compile(r"trakt\.tv/users/([^/]+)/lists/([^/?#]+)")),
    ("trakt", re.compile(r"trakt\.tv/lists/([^/?#]+)")),
    ("mdblist", re.compile(r"mdblist\.com/lists/([^/]+)/([^/?#]+)")),
    ("letterboxd", re.compile(r"letterboxd\.com/([^/]+)/list/([^/?#]+)")),
    ("tvdb", re.compile(r"thetvdb\.com/lists/([^/?#]+)")),
    ("rottentomatoes", re.compile(r"editorial\.rottentomatoes\.com/guide/([^/?#]+)")),
    ("imdb", re.compile(r"imdb\.com/list/(ls\d+)")),
]


def resolve_url(url: str) -> tuple[str, str] | None:
    """A list page URL -> (source, external_id), or None when no adapter's
    contract matches. Nothing is invented: a provider whose list URL shape
    is not stable (or that has no list surface here) reports None."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    for source, pattern in RESOLVERS:
        m = pattern.search(url)
        if not m:
            continue
        groups = m.groups()
        if source == "trakt" and len(groups) == 2:
            return ("trakt", f"{groups[0]}/{groups[1]}")
        if source == "trakt":
            return ("trakt", f"list:{groups[0]}")
        if len(groups) >= 2:
            return (source, f"{groups[0]}/{groups[1]}")
        return (source, groups[0])
    return None


# --- provider enumeration surfaces ------------------------------------------

DISCOVERY_PAGES = 5  # hard page bound per walk
DISCOVERY_MAX_PER_SOURCE = 400

# TMDB has no public list-enumeration surface in API v3: `search/list` is not
# a real endpoint (verified live 2026-10-18: 404) and trending serves media,
# not lists. TMDB therefore reports `no public enumeration surface` like the
# scraped providers, and its catalog rows still come from the curated seed.
REASON_NO_SURFACE = "no public enumeration surface"


def _register(
    source_slug: str, external_id: str, name: str, mediatype: str, url: str
) -> bool:
    """Upsert one discovered list row; returns True when it is new."""
    known = {(row["external_id"]) for row in db.load_lists(source_slug)}
    db.upsert_list(
        source_slug,
        external_id,
        name,
        mediatype,
        False,
        False,
        url,
        ["discovered"],
    )
    return external_id not in known


def _discover_trakt(added: list[str]) -> int:
    t = adapters._Trakt()
    total = 0
    page = 1
    while page <= DISCOVERY_PAGES:
        rows, _ = t._get(f"/lists/trending?page={page}")
        if not isinstance(rows, list):
            break
        for row in rows:
            # Trending rows are {"list": {...}, "user": {...}} envelopes.
            obj = row.get("list") if isinstance(row.get("list"), dict) else row
            ids = obj.get("ids", {})
            lid = str(ids.get("trakt") or "")
            name = obj.get("name") or ""
            if not lid or not name:
                continue
            # The adapter identity is `list:<numeric id>` (its ownerless-list
            # form): fetch_trakt_list maps it to /lists/{id}/items. A bare id,
            # or a slug form for an id-bearing row, breaks the sync contract.
            external_id = f"list:{lid}"
            url = f"https://trakt.tv/lists/{ids.get('slug') or lid}"
            if _register("trakt", external_id, name, "mixed", url):
                added.append(url)
            total += 1
            if total >= DISCOVERY_MAX_PER_SOURCE:
                return total
        if len(rows) < adapters.TRAKT_PAGE_SIZE:
            break
        adapters.sleep_pacing()
        page += 1
    return total


def _discover_mdblist(added: list[str]) -> int:
    """Directory over the MDBList users this catalog already tracks."""
    total = 0
    users = sorted(
        row["external_id"].split("/", 1)[0]
        for row in db.load_lists("mdblist")
        if "/" in row["external_id"]
    )
    key = adapters._key("MDBLIST_API_KEY")
    for user in users[:20]:  # bounded: the tracked users, not a crawl of all users
        try:
            with httpx.Client(timeout=30) as c:
                r = c.get(
                    f"https://api.mdblist.com/lists/user/{user}",
                    params={"apikey": key},
                    follow_redirects=True,
                )
                r.raise_for_status()
                rows = r.json()
        except (httpx.HTTPError, ValueError):
            adapters.record_result("mdblist", False)
            continue
        for row in rows if isinstance(rows, list) else []:
            slug = row.get("slug") or ""
            name = row.get("name") or ""
            if not slug or not name:
                continue
            url = f"https://mdblist.com/lists/{user}/{slug}"
            mediatype = row.get("mediatype") or "mixed"
            if mediatype not in ("movie", "show", "mixed"):
                mediatype = "mixed"
            if _register("mdblist", f"{user}/{slug}", name, mediatype, url):
                added.append(url)
            total += 1
            if total >= DISCOVERY_MAX_PER_SOURCE:
                return total
        adapters.record_result("mdblist", True)
        adapters.sleep_pacing()
    return total


_DISCOVERY_WALKS = {
    "trakt": _discover_trakt,
    "mdblist": _discover_mdblist,
}


def discover_source(slug: str) -> dict:
    """One provider's crawl. A provider whose adapter has no public
    enumeration surface reports honestly as no-surface."""
    if adapters.quarantined(slug):
        return {"source": slug, "skipped": "quarantined", "discovered": 0, "new": 0}
    walk = _DISCOVERY_WALKS.get(slug)
    if walk is None:
        return {
            "source": slug,
            "skipped": REASON_NO_SURFACE,
            "discovered": 0,
            "new": 0,
        }
    added: list[str] = []
    started = time.monotonic()
    try:
        found = walk(added)
    except adapters.SourceError as e:
        adapters.record_result(slug, False)
        return {"source": slug, "error": str(e)[:200], "discovered": 0, "new": 0}
    new = 0
    for url in added:
        resolved = resolve_url(url)
        if resolved and resolved[0] == slug:
            new += 1
    return {
        "source": slug,
        "discovered": found,
        "new": new,
        "elapsed": round(time.monotonic() - started, 1),
    }


def trigger_discover(slug: str) -> threading.Thread:
    """The health board's discover button: register and report; do not import."""

    def run() -> None:
        try:
            discover_source(slug)
        except Exception:  # noqa: BLE001 — a background crawl must not crash the app
            adapters.record_result(slug, False)

    t = threading.Thread(target=run, name=f"discover-{slug}", daemon=True)
    t.start()
    return t


def add_list_by_url(url: str) -> dict:
    """Resolve a pasted list page and register it; syncs once, immediately."""
    resolved = resolve_url(url)
    if resolved is None:
        return {"ok": False, "reason": "no adapter understands this list URL"}
    source_slug, external_id = resolved
    existing = load_list(source_slug, external_id)
    if existing is None:
        name = external_id  # crude but honest until the first sync fetches the title
        db.upsert_list(
            source_slug, external_id, name, "mixed", False, False, url, ["added-by-url"]
        )
        if sync is not None:
            sync.trigger_source(source_slug)
        return {
            "ok": True,
            "source": source_slug,
            "external_id": external_id,
            "created": True,
        }
    return {
        "ok": True,
        "source": source_slug,
        "external_id": external_id,
        "created": False,
    }


def load_list(source_slug: str, external_id: str) -> dict | None:
    for row in db.load_lists(source_slug):
        if row["external_id"] == external_id:
            return row
    return None
