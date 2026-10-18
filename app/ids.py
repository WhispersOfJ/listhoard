"""Arr-usable id resolution for scraped rows (the ListHoard specification §9.2).

Provider ids are translated ON ARRIVAL so the *catalog* carries real,
dedupable ids rather than doing translation per render. Movies resolve
through TMDB /search/movie (title + year); shows through /search/tv.
In-process memoisation keeps repeat syncs cheap; the catalog rows are the
durable cache.
"""

import logging
import os
import re

import httpx

log = logging.getLogger("listhoard.ids")

_TMDB = "https://api.themoviedb.org/3"
_memo: dict[tuple[str, str, int | None], dict] = {}
_miss_memo: set[tuple[str, str, int | None]] = set()


def _key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} missing from environment")
    return v


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def resolve_tmdb_ids(mediatype: str, title: str, year: int | None) -> dict:
    """Best-effort id resolution from TMDB search; {} on no confident match."""
    if mediatype == "show":
        memo_key = ("show", title, year)
        if memo_key in _memo:
            return _memo[memo_key]
        if memo_key in _miss_memo:
            return {}
        params: dict = {"query": title, "api_key": _key("TMDB_API_KEY")}
        if year:
            params["first_air_date_year"] = year
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{_TMDB}/search/tv", params=params)
            r.raise_for_status()
            results = r.json().get("results", [])
        hit = None
        target = _norm(title)
        for res in results:
            if _norm(res.get("name", "")) == target and (
                year is None or int(res.get("first_air_date", "0")[:4] or 0) == year
            ):
                hit = res
                break
        if hit is None and results:
            hit = results[0]
        if hit is None:
            _miss_memo.add(memo_key)
            log.warning(
                "id resolve: no TMDB hit for '%s' (%s) — row skipped", title, year
            )
            return {}
        val = {
            "tmdb_id": hit.get("id"),
            "tvdb_id": hit.get("tvdb_id"),
            "year": int(hit.get("first_air_date", "0")[:4] or 0) or None,
            "poster_path": hit.get("poster_path"),
            "backdrop_path": hit.get("backdrop_path"),
        }
        _memo[memo_key] = val
        return val
    memo_key = ("movie", title, year)
    if memo_key in _miss_memo:
        return {}
    if memo_key in _memo:
        return _memo[memo_key]
    params = {"query": title, "api_key": _key("TMDB_API_KEY")}
    if year:
        params["year"] = year
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_TMDB}/search/movie", params=params)
        r.raise_for_status()
        results = r.json().get("results", [])
    target = _norm(title)
    hit = None
    for res in results:
        if _norm(res.get("title", res.get("original_title", ""))) != target:
            continue
        ry = int(res.get("release_date", "0")[:4] or 0)
        if year is None or ry == year:
            hit = res
            break
    if hit is None and results and year is None:
        # A yearless query defaults to the top result; a year mismatch is a
        # likely wrong title translation and is NOT taken (§9.2 id safety).
        hit = results[0]
    if hit is None:
        _miss_memo.add(memo_key)
        log.warning("id resolve: no TMDB hit for '%s' (%s) — row skipped", title, year)
        return {}
    val = {
        "tmdb_id": hit["id"],
        "tvdb_id": None,
        "year": int(hit.get("release_date", "0")[:4] or 0) or None,
        "poster_path": hit.get("poster_path"),
        "backdrop_path": hit.get("backdrop_path"),
    }
    _memo[memo_key] = val
    return val
