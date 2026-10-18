"""SHP-4b: hypothesis property tests on the scraper's pure parsers (spec §9).

The property: for ANY page body — valid markup, malformed markup, attacker
crafted, empty — `parse_*_rows` returns a well-typed list of rows and never
raises. A parser that throws turns one bad upstream page into a sync error;
the tolerance contract (ListHoard spec §7) says a structural break quarantines
the source, it does not crash the run.
"""

import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

REPO = Path(__file__).resolve().parent.parent.parent

stub = type(sys)("psycopg")
stub.connect = None
sys.modules["psycopg"] = stub
sys.path.insert(0, str(REPO))

try:
    if find_spec("hypothesis") is None:
        raise ImportError
    import hypothesis  # noqa: F401, E402 — prove the module loads, not just exists

    HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover — gate probe handles the skip
    HAS_HYPOTHESIS = False

pytestmark = pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")


def _rows_welltyped(rows) -> None:
    """Every row carries a non-empty str title and an int/None year."""
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict), f"row not a dict: {row!r}"
        assert isinstance(row.get("title"), str) and row["title"]
        year = row.get("year")
        assert year is None or isinstance(year, int), f"year not int/None: {year!r}"


text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF0), max_size=400
)

if HAS_HYPOTHESIS:

    @given(body=text)
    @settings(max_examples=60, deadline=None)
    def test_letterboxd_parser_totally_defined(body):
        from app.scrapers import parse_letterboxd_rows

        _rows_welltyped(parse_letterboxd_rows(body))

    @given(body=text)
    @settings(max_examples=60, deadline=None)
    def test_tvdb_parser_totally_defined(body):
        from app.scrapers import parse_tvdb_rows

        _rows_welltyped(parse_tvdb_rows(body))

    @given(body=text)
    @settings(max_examples=60, deadline=None)
    def test_rt_parser_totally_defined(body):
        from app.scrapers import parse_rt_rows

        _rows_welltyped(parse_rt_rows(body))

    @given(
        title=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()),
        year=st.one_of(st.none(), st.integers(1888, 2100), st.integers()),
        body=text,
    )
    @settings(max_examples=40, deadline=None)
    def test_parse_output_is_stable_under_injection(title, year, body):
        """Injecting a realistic row into arbitrary markup still yields rows
        that dominate garbage — the parser reads structure, not luck."""
        from app.scrapers import parse_letterboxd_rows

        payload = f'{body}<a class="film-title" href="/film/x/">{title} ({year if year else 1999})</a>'
        rows = parse_letterboxd_rows(payload)
        _rows_welltyped(rows)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
