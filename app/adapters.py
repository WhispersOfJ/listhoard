"""Provider adapters (docs/specs/ListHoard.spec.md §7).

Seven provider adapters: the three API providers — TMDB, Trakt, MDBList —
and the four scraped providers — Letterboxd, TVDB, Rotten Tomatoes and IMDb
(listhoard/app/scrapers.py, spec §13 phase 2). IMDb is a recorded gap: it
serves a bot-challenge to this vantage, so it quarantines honestly instead
of pretending to fetch.

Anchors: the arr-consumption MDBList export contract is documented in
docs/specs/ImportLists.spec.md §3 (content negotiation on user agent).
"""

import os
import re
import time
from collections.abc import Callable, Iterator

import httpx

from . import budget

from . import scrapers

_RADARR_UA = "Radarr/4.2.0.2690"
_SONARR_UA = "Sonarr/4.0.20.3012"


def _key(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default or "")
    return v or None


class SourceError(RuntimeError):
    """A provider fetch failed; the sync loop records it and moves on."""


Bit = Callable[..., Iterator[dict]]

TRAKT_PAGE_SIZE = 100  # observed live 2026-10-17 (X-Pagination-Limit: 100)
TRAKT_MAX_PAGES = 500  # 50k rows: a sane bound for even the largest user lists


def _tmdb_get(path: str, **params: str | int) -> dict:
    budget.REGISTRY.get("tmdb").consume()
    key = _key("TMDB_API_KEY")
    if not key:
        raise SourceError("TMDB_API_KEY missing")
    params["api_key"] = key
    with httpx.Client(timeout=30) as c:
        r = c.get(f"https://api.themoviedb.org/3/{path}", params=params)
        try:
            r.raise_for_status()
        except httpx.HTTPError as e:
            # State the path and status, never the request URL: the URL carries
            # the key, and error strings land in the container's logs.
            raise SourceError(f"tmdb {path}: HTTP {r.status_code}") from e
        return r.json()


def _tmdb_image(kind: str, path_key: str, item: dict) -> tuple[str | None, str | None]:
    poster, backdrop = item.get("poster_path"), item.get("backdrop_path")
    return (
        f"tmdb/{kind}/{item['id']}{poster or ''}" if poster else None,
        f"tmdb/{kind}/{item['id']}_b{backdrop or ''}" if backdrop else None,
    )


def _norm_tmdb(item: dict) -> dict:
    title = item.get("title") or item.get("name") or "?"
    date = item.get("release_date") or item.get("first_air_date") or ""
    mt = (
        "movie"
        if "title" in item and item.get("media_type", "movie") != "tv"
        else "show"
    )
    poster = item.get("poster_path")
    backdrop = item.get("backdrop_path")
    return {
        "mediatype": mt,
        "tmdb_id": item["id"],
        "tvdb_id": None,
        "title": title,
        "year": int(date[:4]) if len(date) >= 4 else None,
        "xxx": bool(item.get("adult")),
        "poster_path": poster,
        "backdrop_path": backdrop,
    }


def fetch_tmdb_list(list_id: str) -> Iterator[dict]:
    if list_id == "discover-adult":
        # The validated adult stream: fetch pages, keep only the adult flag rows.
        page = 1
        while page <= 50:  # bounded: 50 pages x 20 = a full first pass
            try:
                d = _tmdb_get(
                    "discover/movie",
                    include_adult="true",
                    sort_by="vote_count.desc",
                    page=page,
                )
            except httpx.HTTPError as e:
                raise SourceError(f"tmdb discover: {e}") from e
            for it in d.get("results", []):
                if it.get("adult"):
                    yield _norm_tmdb(it)
            if page >= d.get("total_pages", 1):
                break
            page += 1
        return
    if not list_id.isdigit():
        raise SourceError(f"tmdb list id not understood: {list_id}")
    page = 1
    while True:
        try:
            d = _tmdb_get(f"list/{list_id}", page=page)
        except httpx.HTTPError as e:
            raise SourceError(f"tmdb list {list_id}: {e}") from e
        for it in d.get("items", []):
            yield _norm_tmdb(it)
        # TMDB returns 20 items per page with no total-count field for lists;
        # stop when a short page arrives.
        if len(d.get("items", [])) < 20:
            break
        page += 1


def _trakt_header() -> dict:
    cid = _key("TRAKT_CLIENT_ID")
    if not cid:
        raise SourceError("TRAKT_CLIENT_ID missing")
    return {"trakt-api-key": cid, "Content-Type": "application/json"}


def _retry_after(headers) -> float:
    """Retry-After in seconds, floored at 2s and capped at 60s."""
    try:
        return min(max(float(headers.get("Retry-After", "2") or 2), 2.0), 60.0)
    except ValueError:
        return 2.0


class _Trakt:
    def __init__(self) -> None:
        self.base = "https://api.trakt.tv"

    def _get(self, path: str) -> tuple[list | dict, httpx.Response]:
        budget.REGISTRY.get("trakt").consume()
        h = _trakt_header()
        with httpx.Client(timeout=30) as c:
            for _attempt in range(8):
                r = c.get(self.base + path, headers=h)
                if r.status_code == 429:
                    # The multi-page walks outpace Trakt's per-key budget
                    # (observed live 2026-10-17). Back off on the header's
                    # own terms and retry the same page; only give up after
                    # 8 spaced retries so a synced walk survives pressure.
                    time.sleep(_retry_after(r.headers))
                    continue
                try:
                    r.raise_for_status()
                except httpx.HTTPError as e:
                    raise SourceError(f"trakt {path}: {e}") from e
                return r.json(), r
            raise SourceError(f"trakt {path}: rate-limit persisted after 8 retries")


def _norm_trakt(obj: dict, mt: str) -> dict:
    trakt_ids = obj.get("ids", {})
    tmdb = trakt_ids.get("tmdb")
    return {
        "mediatype": mt,
        "tmdb_id": tmdb,
        "tvdb_id": obj.get("ids", {}).get("tvdb"),
        "trakt_id": trakt_ids.get("trakt"),
        "title": obj.get("title") or "?",
        "year": (obj.get("year") or None) and int(obj["year"]),
        "xxx": False,
        "poster_path": None,
        "backdrop_path": None,
    }


def fetch_trakt_list(external_id: str) -> Iterator[dict]:
    """external_id forms: `user/slug`, or `list:<numeric>` (ownerless official).

    A bare numeric id is accepted as the same ownerless contract: the
    discovery crawler's earlier registrations carry that form, and it heals
    them without a data migration.
    Walks every page: Trakt serves 100 rows per request and reports the real
    totals in the X-Pagination-* response headers, so a single unpaginated GET
    truncated every list longer than one page to its head rows (that was the
    source of the catalog's 90-99 item bands). The walk stops on page_count,
    on a short page, or on an empty page — whichever happens first — and is
    hard-bounded so a broken pager cannot run away. 429 responses back off
    and retry inside the client, spaced per the Retry-After header.
    """
    t = _Trakt()
    if external_id.startswith("list:") or external_id.isdigit():
        list_id = external_id.split(":")[1] if ":" in external_id else external_id
        path = f"/lists/{list_id}/items"
    else:
        user, slug = external_id.split("/", 1)
        path = f"/users/{user}/lists/{slug}/items"
    page = 1
    while page <= TRAKT_MAX_PAGES:
        items, resp = t._get(f"{path}?page={page}")
        if not isinstance(items, list):
            raise SourceError(f"trakt list {external_id}: unexpected shape")
        for row in items:
            for mt in ("movie", "show"):
                if row.get("type") == mt and mt in row:
                    yield _norm_trakt(row[mt], mt)
        raw = resp.headers.get("X-Pagination-Page-Count")
        total_pages: int | None = None
        if raw:
            try:
                total_pages = int(raw)
            except ValueError:
                total_pages = None  # unusable header: fall back to the row checks
        if total_pages is not None and page >= total_pages:
            break
        if not items or len(items) < TRAKT_PAGE_SIZE:
            break  # short or empty page: the pager is exhausted
        page += 1


def _mdblist_get(url: str, mediatype: str) -> list[dict]:
    budget.REGISTRY.get("mdblist").consume()
    ua = _RADARR_UA if mediatype == "movie" else _SONARR_UA
    with httpx.Client(timeout=30) as c:
        r = c.get(url, headers={"User-Agent": ua}, follow_redirects=True)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise SourceError(f"mdblist {url}: non-JSON response ({e})") from e


def fetch_mdblist_list(external_id: str) -> Iterator[dict]:
    """Content-negotiated export (ImportLists spec §3): movie rows to a Radarr
    UA, show rows to a Sonarr UA. The first media type that yields rows wins;
    a list that yields neither is an error raised, not silence."""
    url = f"https://mdblist.com/lists/{external_id}"
    seen, out = set(), []
    for mt in ("movie", "show"):
        rows = _mdblist_get(url, mt)
        for it in rows or []:
            ident = mt, it.get("title")
            if ident in seen:
                continue
            seen.add(ident)
            out.append(
                {
                    "mediatype": mt,
                    "tmdb_id": it.get("tmdbid"),
                    "tvdb_id": it.get("tvdbid"),
                    "title": it.get("title") or "?",
                    "year": (it.get("year") or None) and int(it["year"]),
                    "xxx": bool(it.get("adult")),
                    "poster_path": None,
                    "backdrop_path": None,
                }
            )
        if out:
            break
    yield from out


def fetch_letterboxd(external_id: str) -> Iterator[dict]:
    """One film-details request per row (pacing inside the scraper); rows are
    yielded after the full walk so a mid-walk failure still lands the catalog."""
    rows = scrapers.fetch_letterboxd_list(external_id)
    record_result("letterboxd", True)
    yield from rows


def fetch_tvdb(external_id: str) -> Iterator[dict]:
    rows = scrapers.fetch_tvdb_list(external_id)
    record_result("tvdb", True)
    yield from rows


def fetch_rottentomatoes(external_id: str) -> Iterator[dict]:
    rows = scrapers.fetch_rottentomatoes_list(external_id)
    record_result("rottentomatoes", True)
    yield from rows


def fetch_imdb(external_id: str) -> Iterator[dict]:
    scrapers.fetch_imdb_list(external_id)  # always raises; quarantine bites here
    yield from ()


_ADAPTERS_API: dict[str, Bit] = {
    "tmdb": fetch_tmdb_list,
    "trakt": fetch_trakt_list,
    "mdblist": fetch_mdblist_list,
}

ADAPTERS: dict[str, Bit] = {
    **_ADAPTERS_API,
    "letterboxd": fetch_letterboxd,
    "tvdb": fetch_tvdb,
    "rottentomatoes": fetch_rottentomatoes,
    "imdb": fetch_imdb,
}

_QUARANTINE_AFTER = 5  # consecutive failures before the scheduler skips a source
_quarantine: dict[str, int] = {}


def quarantined(slug: str) -> bool:
    return _quarantine.get(slug, 0) >= _QUARANTINE_AFTER


def record_result(slug: str, ok: bool) -> None:
    _quarantine[slug] = 0 if ok else _quarantine.get(slug, 0) + 1


PACING_SECONDS = 1.0  # one request/second adaptive floor (spec §7)


def fetch_list(source_slug: str, external_id: str) -> Iterator[dict]:
    fn = ADAPTERS.get(source_slug)
    if fn is None:
        raise SourceError(
            f"source '{source_slug}' is not implemented yet (spec §13 phase 2)"
        )
    yield from fn(external_id)


def sleep_pacing() -> None:
    time.sleep(PACING_SECONDS)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
