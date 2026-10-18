"""Seed catalog loading.

Loads `scripts/listhoard/seed/*.json` (mounted read-only at /app/seed) into the
catalog: the committed research artifact of docs/specs/ListHoard.spec.md §8.
The loader is additive and idempotent: re-running produces no duplicates, and
a row the JSON no longer mentions is left untouched.

Shippability (docs/specs/Shippability.spec.md §10, ruling 8): a fresh public
instance ships an **empty catalog** — the seed load is a first-run option, not
a boot step. `LISTHOARD_SEED_ON_BOOT=off` (the default) registers the source
slugs only and skips the private research seed; `on` preserves this home
deployment's behavior exactly.
"""

import glob
import json
import os

from . import db

SOURCES = {
    "tmdb": "api",
    "trakt": "api",
    "mdblist": "api",
    "letterboxd": "scrape",
    "imdb": "scrape",
    "rottentomatoes": "scrape",
    "tvdb": "scrape",
}
MEDIATYPES = {"movie", "show", "mixed"}
KEYS = {"source", "external_id", "name", "mediatype", "anime", "xxx", "tags", "url"}


def register_sources() -> None:
    """Register the built-in source slugs without loading any seed rows.
    The catalog stays empty (shippability ruling 8); the adapters and the
    health board both key off these slugs."""
    for slug, kind in SOURCES.items():
        db.ensure_source(slug, kind)


def seed_on_boot() -> bool:
    """The resolved boot-time seed switch: on only when explicitly opted in."""
    return os.environ.get("LISTHOARD_SEED_ON_BOOT", "off") == "on"


def validate_seed_file(path: str) -> list[str]:
    """Return the list of validation errors; empty means the file is clean."""
    errors = []
    with open(path) as f:
        doc = json.load(f)
    if doc.get("schema") != 1:
        errors.append(f"{path}: schema must be 1")
    lists = doc.get("lists", [])
    seen: set[str] = set()
    for row in lists:
        if set(row) != KEYS:
            errors.append(
                f"{path}: {row.get('external_id')}: wrong key set: {sorted(row)}"
            )
            continue
        key = f"{row['source']}/{row['external_id']}"
        if key in seen:
            errors.append(f"{path}: duplicate (source, external_id) {key}")
        seen.add(key)
        if row["source"] not in SOURCES:
            errors.append(f"{path}: unknown source {row['source']}")
        if row["mediatype"] not in MEDIATYPES:
            errors.append(f"{path}: bad mediatype {row['mediatype']!r}")
        for flag in ("anime", "xxx"):
            if not isinstance(row[flag], bool):
                errors.append(f"{path}: {key}: {flag} is not boolean")
        if not row["name"]:
            errors.append(f"{path}: {key}: empty name")
        if "None" in row["url"]:
            errors.append(f"{path}: {key}: malformed url")
    return errors


def load_seed(seed_dir: str | None = None) -> dict:
    """Validate, then upsert every seed entry. Returns counts."""
    path_list = sorted(glob.glob(os.path.join(seed_dir or "/app/seed", "*.json")))
    errors: list[str] = []
    for p in path_list:
        errors.extend(validate_seed_file(p))
    if errors:
        raise RuntimeError("seed catalog failed validation: " + "; ".join(errors[:10]))
    for slug, kind in SOURCES.items():
        db.ensure_source(slug, kind)
    loaded = 0
    for p in path_list:
        with open(p) as f:
            doc = json.load(f)
        for row in doc["lists"]:
            db.upsert_list(
                row["source"],
                row["external_id"],
                row["name"],
                row["mediatype"],
                row["anime"],
                row["xxx"],
                row.get("url"),
                row["tags"],
            )
            loaded += 1
    return {"files": len(path_list), "lists": loaded}
