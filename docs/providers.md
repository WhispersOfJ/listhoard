# Providers

ListHoard pulls lists from these providers. Each one walks the same adapter
interface: authenticate, enumerate, fetch, map items to `(media type, TMDB id,
TVDB id)` rows.

| Provider | Access | Key needed? | Notes |
|---|---|---|---|
| TMDB | Official API v3 | **Yes — the only must-have** (free signup) | List + collection + trending endpoints |
| Trakt | Official API, client id only | Optional | Public list reads need no OAuth |
| MDBList | Official export per user/list | Optional | Same export contract the arr import-list pipeline uses; degrades to "source unavailable" on the health board without a key |
| Letterboxd | Scrape (no public API) | No | robots-respecting, adaptive pacing, backoff |
| IMDb | Scrape of list/chart pages + public datasets | No | IMDb ids translate to TMDB/TVDB once, cached |
| Rotten Tomatoes | Scrape of awards/rank pages | No | Structure drifts often; the health board catches it |
| TVDB | API v4 or scrape fallback | Optional | Researched at bring-up; falls back to scraping that provider's curated rank pages if the v4 pin model blocks |

## Keys and degradation

- **TMDB is the only required key** — a fresh free signup takes about a minute;
  the in-app setup links the signup page directly.
- Trakt, MDBList and TVDB keys are optional: without one, the provider works
  key-less where that is possible, or reports "source unavailable" honestly on
  the health board. A missing key never produces placeholder demo data.
- Scraped providers (Letterboxd, IMDb, Rotten Tomatoes) need no key at all.
- If you enable the explicit-content module (`LISTHOARD_ENABLE_XXX=on`),
  its scraped adult-list provider joins the queue as a standard adapter; left
  off, nothing adult is registered, rendered or routed — the UI toggle never
  renders and the adult flow is absent from the app entirely.

## Politeness

Every request a source makes passes through a per-provider budget
(`LISTHOARD_BUDGET_<SLUG>_PER_HOUR` / `_PER_DAY`; per-provider defaults sit at each
API's documented quota and ship in the compose file's comments).
When a budget is spent, the source pauses — it is reported on the health
board with used/limit and resumes in the next window; it is never marked
failing. Scraped providers honour `robots.txt` with a 1 req/s adaptive floor
and exponential backoff.
