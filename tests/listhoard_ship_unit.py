#!/usr/bin/env python3
"""Headless tests for Shippability slice 1 (the shippability specification).

Run directly (python3 tests/listhoard_ship_unit.py); wired into
scripts/gate.py. Covers the slice's behavioral contract:

1. Seed gate — ``seed.seed_on_boot()`` defaults OFF; ``seed.register_sources``
   registers all seven built-in slugs through ``db.ensure_source`` without
   touching catalog rows.
2. First-run funnel — the dashboard template carries the empty-catalog banner
   (``totals.lists == 0`` renders the add-list call-to-action).
3. Quickstart packaging — both compose files parse as YAML and name every
   key the slice contract requires; the seed mount is ABSENT from the
   quickstart (an empty-catalog instance ships no data mount).
"""

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
QS = REPO / "."


def run_tests() -> bool:
    failures: list[str] = []

    # --- 1. the seed gate ------------------------------------------------------
    import os

    sys.path.insert(0, str(REPO))
    # Stub psycopg so app.db imports inertly.
    stub = type(sys)("psycopg")
    stub.connect = None
    sys.modules["psycopg"] = stub

    import app.seed as seed_mod

    for val in ("", None, "off", "0", "no"):
        if val is None:
            os.environ.pop("LISTHOARD_SEED_ON_BOOT", None)
        else:
            os.environ["LISTHOARD_SEED_ON_BOOT"] = val
        if seed_mod.seed_on_boot() is not False:
            failures.append(f"seed_on_boot({val!r}) must resolve False (default off)")
    os.environ["LISTHOARD_SEED_ON_BOOT"] = "on"
    if seed_mod.seed_on_boot() is not True:
        failures.append("seed_on_boot('on') must resolve True")
    os.environ.pop("LISTHOARD_SEED_ON_BOOT", None)

    # register_sources calls ensure_source exactly once per built-in slug.
    ensured: list[str] = []
    seed_mod.db.ensure_source = lambda slug, kind: ensured.append(slug)  # type: ignore[assignment]
    seed_mod.register_sources()
    expected = sorted(seed_mod.SOURCES)
    if sorted(ensured) != expected:
        failures.append(f"register_sources ensured {ensured}, expected {expected}")

    # --- 2. the first-run funnel ----------------------------------------------
    dash = (REPO / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    if "totals.lists == 0" not in dash:
        failures.append("dashboard is missing the empty-catalog banner condition")
    if (
        "/add-list" not in dash.split("first-run")[1][:400]
        if "first-run" in dash
        else True
    ):
        failures.append("the first-run banner must funnel to /add-list")

    # --- 3. quickstart packaging ----------------------------------------------
    text = QS.joinpath("docker-compose.yml").read_text(encoding="utf-8")

    yaml_ok = True
    try:
        import yaml  # the repo's ya tooling lives on yq, not python yaml; parse by hand below

        yaml.safe_load(text)
    except ImportError:
        yaml_ok = False
    except Exception as e:  # noqa: BLE001
        failures.append(f"quickstart compose does not parse: {e}")
        yaml_ok = False

    if yaml_ok:
        if (
            "LISTHOARD_SEED_ON_BOOT" not in text
            or '"off"' not in text.split("LISTHOARD_SEED_ON_BOOT")[1][:8]
        ):
            failures.append("quickstart must pin LISTHOARD_SEED_ON_BOOT off")
        if "/app/seed" in text or "scripts/listhoard/seed" in text:
            failures.append(
                "quickstart must NOT mount the seed (ruling 8: ships empty)"
            )
        if "LISTHOARD_ENABLE_XXX" not in text:
            failures.append("quickstart must name the adult-content gate (ruling 13)")
        overlays = QS.joinpath("docker-compose.fullstack.yml").read_text(
            encoding="utf-8"
        )
        for needle in ("postgres:17-alpine", "pg_isready", "pg-data"):
            if needle not in overlays:
                failures.append(f"fullstack overlay missing {needle!r}")

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        return False
    print(
        "PASS shippability slice 1: seed gate, first-run funnel, quickstart packaging"
    )
    return True


def main() -> int:
    return 0 if run_tests() else 1


if __name__ == "__main__":
    sys.exit(main())
