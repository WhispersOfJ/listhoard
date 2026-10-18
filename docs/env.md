# Configuration

Everything lives in `.env` — copy `.env.example` from the repo root and fill.
Real secrets never go in a committed file; the example file carries
placeholders only.

## Required

| Key | Meaning |
|---|---|
| `TMDB_API_KEY` | Free [TMDB](https://www.themoviedb.org/signup) v3 key — the only must-have |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Your postgres, or the fullstack overlay's embedded one (`POSTGRES_HOST=postgres`) |
| `TZ` | IANA timezone, e.g. `Europe/Berlin` |

## Your arrs (optional — imports need one)

| Key | Meaning |
|---|---|
| `RADARR_BASE_URL` / `RADARR_API_KEY` | Radarr for movies |
| `SONARR_BASE_URL` / `SONARR_API_KEY` | Sonarr for shows |

## Your Jellyfin (optional — powers auth + badges)

| Key | Meaning |
|---|---|
| `JELLYFIN_BASE_URL` / `JELLYFIN_API_KEY` | Jellyfin gives in-library badges and, in `jellyfin` auth mode, the account system to log in with. Absent = degraded badges, not broken pages. |

## Auth modes

| Key | Meaning |
|---|---|
| `LISTHOARD_AUTH_MODE` | `jellyfin` \| `local` \| `token`. Empty = the first-run setup decides. |
| `LISTHOARD_ADMIN_TOKEN` | Shared secret for `token` mode |
| `LISTHOARD_LOCAL_ADMIN_USER` / `LISTHOARD_LOCAL_ADMIN_PASSWORD` | Credentials for `local` mode |

## Behaviour knobs

| Key | Default | Meaning |
|---|---|---|
| `LISTHOARD_SEED_ON_BOOT` | `off` | The curated starter loads by explicit choice (a first-run option), never automatically — a fresh instance ships an empty catalog |
| `LISTHOARD_ENABLE_XXX` | `off` | Explicit-content module: when off, the module is not registered, the toggle never renders, and the adult import flow is absent entirely |
| `LISTHOARD_HUGE_LIST_THRESHOLD` | `5000` | Imports of lists above this default to search-on-add off (still per-import overridable) |
| `LISTHOARD_BUDGET_<SLUG>_PER_HOUR` | documented quota | Per-provider request budget, hour window — slugs: `tmdb`, `trakt`, `mdblist`, and each scraper |
| `LISTHOARD_BUDGET_<SLUG>_PER_DAY` | documented quota | Same, day window |
| `LISTHOARD_SLOW_REQUEST_MS` | `1000` | Warn when a page render exceeds this |
| `LISTHOARD_SLOW_QUERY_MS` | `500` | Warn when a database statement exceeds this |

## Optional provider keys

| Key | Meaning |
|---|---|
| `TRAKT_CLIENT_ID` | Trakt reads (public lists only) |
| `MDBLIST_API_KEY` | MDBList export path |
| `TVDB_API_KEY` / `TVDB_SUBSCRIPTION_PIN` | Only if driving TVDB v4 instead of the scrape fallback |
