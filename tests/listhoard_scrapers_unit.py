#!/usr/bin/env python3
"""Headless tests for the ListHoard scraped adapters (spec §7 phase 2).

Run directly (python3 tests/listhoard_scrapers_unit.py); wired into
scripts/gate.py. Never touches the network: the parse helpers are exercised
against small fixtures copied from the live markup captured on 2026-10-17,
so the regexes cannot drift from the pages they were written against.

httpx is injected as a stub because the host/CI environments do not install
application dependencies (listhoard ships its own image); the stub exists
only so the modules import — the pure parsers never call into it.
"""

import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_app():
    """Import listhoard's app package with a stub httpx in place. The stub
    must cover every symbol the modules reference in *eager* annotations
    (Python ≤3.12): Client, Response, HTTPError."""
    stub = types.ModuleType("httpx")
    stub.Client = object
    stub.Response = object
    stub.HTTPError = Exception
    sys.modules.setdefault("httpx", stub)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


# Letterboxd: the real deployed row shape — one slug attr per row followed
# by its `Title (Year)` name attr; HTML-escaped titles unescape; dupes drop.
LB_PAGE = """
<div data-item-name="Psycho (1960)" data-details-endpoint="/film/psycho/json/"></div>
<div data-item-name="2001: A Space Odyssey (1968)" data-details-endpoint="/film/2001-a-space-odyssey/json/"></div>
<div data-item-name="Schindler&#039;s List (1993)" data-details-endpoint="/film/schindlers-list/json/"></div>
<div data-item-name="Psycho (1960)" data-details-endpoint="/film/psycho/json/"></div>
<div data-item-name="Strll&#039;s Oddity" data-details-endpoint="/film/strlls-oddity/json/"></div>
"""

# TVDB: real deployed row shape — one poster img carrying the numeric id,
# plus one artless row whose title must survive with tvdb_id None.
TVDB_PAGE = """
<h3 class="mt-0 mb-0"><a href="/series/marvels-agents-of-shield">Marvel&#039;s Agents of S.H.I.E.L.D.</a></h3>
<div class="row">
    <div class="col-xs-12 col-sm-3 col-md-2 mt-2">
        <a href="/series/marvels-agents-of-shield">
            <img class="img-responsive img-rounded"
                 src="https://artworks.thetvdb.com/banners/series/263365/posters/5f03f1ff801ec.jpg">
        </a>
    </div>
</div>
<h3 class="mt-0 mb-0"><a href="/series/marvels-agent-carter">Marvel&#039;s Agent Carter</a></h3>
<div class="row">
    <div class="col-xs-12 col-sm-3 col-md-2 mt-2">
        <a href="/series/marvels-agent-carter">
            <img class="img-responsive img-rounded"
                 src="https://artworks.thetvdb.com/banners/v4/series/341455/posters/62a1.jpg">
        </a>
    </div>
</div>
<h3 class="mt-0 mb-0"><a href="/series/marvels-fake-artless">Artless Show</a></h3>
"""

# Rotten Tomatoes: the meta-title anchor is followed by a meta-year span that
# carries the FULL four-digit year (a two-digit capture would read "19").
RT_PAGE = """
<a class="meta-title" href="https://www.rottentomatoes.com/m/the_godfather">The Godfather</a>
<span class="meta-year">(1972)</span>
<a class="meta-title" href="https://www.rottentomatoes.com/m/seven_samurai_1956">Seven Samurai</a>
<span class="meta-year">(1954)</span>
<a class="meta-title" href="https://www.rottentomatoes.com/m/seven_samurai_1956">Seven Samurai</a>
<span class="meta-year">(1954)</span>
"""


def main() -> int:
    _load_app()
    from app import adapters, ids, scrapers

    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    # 1. Letterboxd row extraction: order kept, pairs fused, escapes
    # unescaped, an unyeared title still parses, duplicates drop.
    rows = scrapers.parse_letterboxd_rows(LB_PAGE)
    check(
        "letterboxd rows",
        rows
        == [
            {"slug": "psycho", "title": "Psycho", "year": 1960},
            {
                "slug": "2001-a-space-odyssey",
                "title": "2001: A Space Odyssey",
                "year": 1968,
            },
            {"slug": "schindlers-list", "title": "Schindler's List", "year": 1993},
            {"slug": "strlls-oddity", "title": "Strll's Oddity", "year": None},
        ],
    )

    # 2. TVDB slug-keyed pairing, artless rows survive, ids are ints.
    rows = scrapers.parse_tvdb_rows(TVDB_PAGE)
    check("tvdb row count", len(rows) == 3)
    check(
        "tvdb first row",
        rows[0]
        == {
            "tvdb_id": 263365,
            "title": "Marvel's Agents of S.H.I.E.L.D.",
            "slug": "marvels-agents-of-shield",
        },
    )
    check("tvdb v4 banner parsed", rows[1]["tvdb_id"] == 341455)
    check(
        "tvdb artless row keeps title",
        rows[2]
        == {
            "tvdb_id": None,
            "title": "Artless Show",
            "slug": "marvels-fake-artless",
        },
    )

    # 3. RT title + FULL-year capture, deduped case-insensitively.
    rt = scrapers.parse_rt_rows(RT_PAGE)
    check("rt row count", len(rt) == 2)
    check("rt full year", rt[0] == {"title": "The Godfather", "year": 1972})
    check("rt second row", rt[1] == {"title": "Seven Samurai", "year": 1954})

    # 4. All seven catalogued sources have adapters, matching the seed's SOURCES.
    check(
        "adapters registry",
        sorted(adapters.ADAPTERS)
        == sorted(
            {"tmdb", "trakt", "mdblist", "letterboxd", "imdb", "rottentomatoes", "tvdb"}
        ),
    )

    # 5. IMDb fails loudly (the recorded 202-challenge gap), never silently.
    try:
        list(adapters.fetch_list("imdb", "ls006266261"))
        failures.append("imdb must raise, not yield")
    except adapters.SourceError:
        pass

    # 6. The ids normaliser matches the §9.2 comparison semantics.
    check(
        "ids norm",
        ids._norm("Marvel's Agents of S.H.I.E.L.D.") == "marvelsagentsofshield",
    )

    if failures:
        print("FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("scrapers unit: 6/6 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
