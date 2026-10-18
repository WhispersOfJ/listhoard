# Changelog

All notable user-visible changes to ListHoard, keeping a changelog-style
format. The app follows semver: `0.x` is the shippability ramp; `1.0.0` is
public readiness.

## 0.2.0 — 2026-10-18

### Added

- **Saved sights** — name and keep the browse filters as localStorage-backed
  chips; a sight is a plain filtered link the server renders, never a
  client-side view.
- **The published image** — `ghcr.io/whispersofj/listhoard:0.2.0` (multi-arch,
  built by the repo's own workflow on every version tag); the quickstart
  compose pins it. Pull-to-run replaces clone-and-build.
- **A testable repo** — the app's headless test suites ship inside the repo
  (`pip install -r requirements-dev.txt && python3 tests/listhoard_ship_unit.py`
  or `python3 -m pytest tests/pytest_app/`), so contributions can validate
  themselves.
- **A public changelog** — this file; `upgrading`'s reference now resolves.
- **Docs site with real screenshots** — dashboard, browse, list detail,
  import controls and the health board, rebuilt on every main push.

### Fixed

- An arr-less deployment no longer breaks on a list page: the app degrades
  honestly — a wiring notice replaces the import form, badges say
  not-in-library, and nothing calls a dead hostname.

## 0.1.0 — 2026-10-18

The first release, cut from the shippability baseline (slices 1–4).

### Added

- **The hoard** — seven providers (TMDB, Trakt, MDBList, Letterboxd, IMDb,
  Rotten Tomatoes, TVDB) sync into one deduplicated Postgres catalog;
  every poster is cached on disk; the browser never touches a provider CDN.
- **Quickstart** — ten-minute install: `.env.example`, the compose stack,
  and an embedded-postgres fullstack overlay for arr-less users.
- **First-run funnel** — a fresh instance ships an empty catalog (no bundled
  seed data); paste any provider's list page into *add-list by URL* and the
  first sync fills it. Discovery crawls (Trakt trending, MDBList directory)
  land rows tagged `discovered`, imported only by explicit curation.
- **Pluggable auth** — `jellyfin`, `local`, or `token` modes backed by
  Postgres sessions that survive restarts; auth degrades to badges, not
  broken pages, when Jellyfin is absent.
- **Polite sync** — per-provider hour/day request budgets with env-tunable
  ceilings; a spent budget *pauses* the source (health board shows
  used/limit) instead of erroring it.
- **Imports and un-imports** — one-click imports into Radarr/Sonarr with the
  full add-option surface, per-import tags, anime `seriesType`, huge-list
  search-on-add default-off, per-item curation, resolvability flags,
  in-library badges, and a gated tag-keyed un-import.
- **Docs** — a mkdocs site (providers, env reference, troubleshooting,
  upgrading) published automatically on every main push.

### Fixed

- `/logout` crashed (500) since first ship — it called a nonexistent
  response-cookie API.
- The dashboard's syncing-depth bar divided by zero on an empty catalog
  (the state a fresh install starts in).
