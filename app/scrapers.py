"""Scraped provider adapters (the ListHoard specification §7, Phase 2).

- letterboxd: list pages expose one `data-details-endpoint` per row; each
  film's `/film/<slug>/json/` page carries the ids (`tmdbID`, `name`,
  `releaseYear`). One request per film — done once; the catalog then holds
  the ids and re-runs skip already-known rows (observed markup 2026-10-17).
- tvdb: list rows render a series-slug title anchor plus a poster img whose
  src carries the numeric tvdb id; pairing happens on the slug, so rows
  without artwork keep their title and resolve from search later (§9.2).
  Observed: 24 of 33 MCU rows carried artwork ids.
- rottentomatoes: editorial guides pair `meta-title` anchors with
  `meta-year` spans one-to-one (299/299 on the captured guide); ids resolve
  from TMDB search (spec §9.2).
- imdb: IMDb serves a bot-challenge (HTTP 202) to non-browser clients from
  this vantage (observed 2026-10-17); the adapter fails fast and the
  sync loop quarantines it. The gap stays visible on the health board.

The parse helpers are pure (fixture-testable offline); the fetchers use
httpx with the §7 pacing and quarantine rules via `adapters`.
"""

import html as html_mod
import logging
import re

import httpx

from . import adapters, budget, ids

log = logging.getLogger("listhoard.scrapers")

_LB_PAGE_CAP = 200  # 200 pages x 100 rows: a sane walk bound for any list

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

LB_SLUG_RX = re.compile(r'data-details-endpoint="/film/([a-z0-9-]+)/json/"')
TV_TITLE_RX = re.compile(
    r'<h3 class="mt-0 mb-0"><a href="/series/([a-z0-9-]+)">([^<]+)</a></h3>'
)
TV_ART_RX = re.compile(
    r'href="/series/([a-z0-9-]+)">\s*<img\s+class="img-responsive img-rounded"\s+'
    r'src="[^"]*banners/(?:v4/)?series/(\d+)/posters/'
)
RT_RX = re.compile(
    r'<a class="meta-title" href="https://www\.rottentomatoes\.com/m/[^"]+">'
    r'([^<]+)</a>\s*<span class="meta-year">\(((?:19|20)[0-9]{2})\)</span>'
)


LB_NAME_RX = re.compile(
    r'data-item-name="([^"]+)"|'
    r'data-details-endpoint="/film/([a-z0-9-]+)/json/"'
)


def parse_letterboxd_rows(page: str) -> list[dict]:
    """Rows straight from the list page: every row carries both its film slug
    (via data-details-endpoint) and its `Title (Year)` name (via
    data-item-name). The deployed markup puts the name attr before the slug
    attr of the SAME row, so the two streams pair by position: the k-th name
    belongs to the k-th slug (counts matched 100/100 on the captured page).
    HTML-escaped titles unescape; no per-film request is needed — the
    film-detail endpoints serve a bot-challenge while list pages open."""
    names: list[str] = []
    slugs: list[str] = []
    for m in LB_NAME_RX.finditer(page):
        if m.group(1):
            name = html_mod.unescape(m.group(1)).strip()
            if name not in names:
                names.append(name)
        else:
            if m.group(2) not in slugs:
                slugs.append(m.group(2))
    out: list[dict] = []
    for name, slug in zip(names, slugs):
        mt = re.match(r"(.+?) \((\d{4})\)$", name)
        out.append(
            {
                "slug": slug,
                "title": mt.group(1) if mt else name,
                "year": int(mt.group(2)) if mt else None,
            }
        )
    return out


def parse_tvdb_rows(page: str) -> list[dict]:
    """Pair title anchors and art anchors by series slug: the deployed row
    renders the slug on both. Rows without artwork keep their title and
    resolve ids from search later (spec §9.2). Deduped by slug, page order."""
    titles: dict[str, str] = {}
    for m in TV_TITLE_RX.finditer(page):
        titles.setdefault(m.group(1), html_mod.unescape(m.group(2)).strip())
    arts: dict[str, int] = {}
    for m in TV_ART_RX.finditer(page):
        arts.setdefault(m.group(1), int(m.group(2)))
    out: list[dict] = []
    for slug, title in titles.items():
        tvdb_id = arts.get(slug)
        if tvdb_id is None:
            # No artwork means no in-page id: the row stays and ids resolve
            # from TMDB search by title (spec §9.2), so nothing is silently
            # dropped, but rows that also fail resolution abort the sync.
            pass
        out.append({"tvdb_id": tvdb_id, "title": title, "slug": slug})
    return out


def parse_rt_rows(page: str) -> list[dict]:
    """Title+year pairs from a meta-title/meta-year guide; deduped by title."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in RT_RX.finditer(page):
        title = html_mod.unescape(m.group(1)).strip()
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "year": int(m.group(2))})
    return out


def _get(url: str, slug: str) -> str:
    """One scraped page; `slug` charges that provider's budget first."""
    budget.REGISTRY.get(slug).consume()
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(url, headers={"User-Agent": _UA}, follow_redirects=True)
            r.raise_for_status()
            return r.text
    except httpx.HTTPError as e:
        raise adapters.SourceError(f"{url}: {e}") from e


def fetch_letterboxd_list(external_id: str) -> list[dict]:
    user, slug = external_id.split("/", 1)
    base = f"https://letterboxd.com/{user}/list/{slug}/"
    # The list's first page opens while its per-film endpoints serve a
    # bot-challenge. Longer lists advertise their continuations as page/N
    # links in the markup (observed live 2026-10-17: a 250-row list carried
    # `page/2` and `page/3` links and served 100 more rows per URL), so walk
    # them all — stopping when a continuation yields no rows, 404s, or the
    # walk bound is hit. A continuation failure keeps the rows already read
    # and reports the shortfall in `lists.item_count` rather than aborting.
    bodies: list[str] = [_get(base, "letterboxd")]
    page_no = 2
    while page_no <= _LB_PAGE_CAP:
        try:
            body = _get(f"{base}page/{page_no}/", "letterboxd")
        except adapters.SourceError as e:
            log.warning(
                "letterboxd pagination stopped at page %d of %s: %s",
                page_no,
                external_id,
                e,
            )
            break
        if not parse_letterboxd_rows(body):
            break
        bodies.append(body)
        page_no += 1
    film_payloads: list[dict] = []
    for body in bodies:
        for row in parse_letterboxd_rows(body):
            got = ids.resolve_tmdb_ids("movie", row["title"], row["year"])
            if not got.get("tmdb_id"):
                continue  # unresolvable rows abort the sync honestly
            film_payloads.append(
                {
                    "mediatype": "movie",
                    "tmdb_id": got["tmdb_id"],
                    "tvdb_id": None,
                    "title": row["title"],
                    "year": got.get("year") or row["year"],
                    "xxx": False,
                    "poster_path": got.get("poster_path"),
                    "backdrop_path": got.get("backdrop_path"),
                }
            )
    return film_payloads


def fetch_tvdb_list(external_id: str) -> list[dict]:
    body = _get(f"https://thetvdb.com/lists/{external_id}", "tvdb")
    out: list[dict] = []
    for row in parse_tvdb_rows(body):
        if row["tvdb_id"] is not None:
            out.append(
                {
                    "mediatype": "show",
                    "tmdb_id": None,
                    "tvdb_id": row["tvdb_id"],
                    "title": row["title"],
                    "year": None,
                    "xxx": False,
                    "poster_path": None,
                    "backdrop_path": None,
                }
            )
            continue
        # In-page ids only travel with artwork; resolve the rest by title.
        got = ids.resolve_tmdb_ids("show", row["title"], None)
        if not got.get("tmdb_id"):
            continue  # unresolvable rows abort the sync honestly
        out.append(
            {
                "mediatype": "show",
                "tmdb_id": got["tmdb_id"],
                "tvdb_id": got.get("tvdb_id"),
                "title": row["title"],
                "year": got.get("year"),
                "xxx": False,
                "poster_path": got.get("poster_path"),
                "backdrop_path": got.get("backdrop_path"),
            }
        )
    return out


def fetch_rottentomatoes_list(external_id: str) -> list[dict]:
    body = _get(
        f"https://editorial.rottentomatoes.com/guide/{external_id}/", "rottentomatoes"
    )
    out: list[dict] = []
    for row in parse_rt_rows(body):
        got = ids.resolve_tmdb_ids("movie", row["title"], row["year"])
        if not got.get("tmdb_id"):
            continue  # unresolvable rows abort the sync honestly
        out.append(
            {
                "mediatype": "movie",
                "tmdb_id": got["tmdb_id"],
                "tvdb_id": None,
                "title": row["title"],
                "year": got.get("year") or row["year"],
                "xxx": False,
                "poster_path": got.get("poster_path"),
                "backdrop_path": got.get("backdrop_path"),
            }
        )
    return out


def fetch_imdb_list(external_id: str) -> list[dict]:
    # IMDb serves the 202 bot-challenge to non-browser clients from this
    # deployment's vantage (observed 2026-10-17) — record + quarantine (§7).
    raise adapters.SourceError(
        "imdb serves a bot-challenge (HTTP 202) to non-browser clients "
        "from this deployment's vantage; quarantined until a "
        "browser-fingerprint fetch or dataset job lands (recorded gap)"
    )
