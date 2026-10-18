# Troubleshooting

Symptom-first, one command each.

| Symptom | First check |
|---|---|
| Dashboard empty, nothing added | `docker compose logs listhoard` right after your first sync — a source that failed to fetch names its error on the health board, which the dashboard renders |
| A source shows `budget exhausted` | Expected honest pause, not an error: the source hit its per-provider request budget and will resume in the next window. Raise the window's ceiling via `LISTHOARD_BUDGET_<SLUG>_PER_HOUR` if your quota allows |
| A source shows `unavailable` | That provider's optional key is missing or invalid; check [Providers](providers.md) for which key applies |
| A scrape source stays stale after a site change | Expected: structural break quarantines that one source and the board names the last error; other sources continue; the stale contents stay visible until the next successful sync |
| Import button does nothing | Check the arr is reachable and `RADARR_BASE_URL` / `SONARR_BASE_URL` respond from inside the container; the import dialog names the lookup failure per row |
| Login fails | Auth mode mismatch — see [Configuration](env.md#auth-modes); `token` mode needs `LISTHOARD_ADMIN_TOKEN`, `local` mode needs `LISTHOARD_LOCAL_ADMIN_*` |
| Page loads slow | Tune both warning thresholds: `LISTHOARD_SLOW_REQUEST_MS` / `LISTHOARD_SLOW_QUERY_MS`; the logs carry the explicit `slow request` / `slow query` lines past them |

## Container won't start

```bash
docker compose logs listhoard | tail -50
```

1. `.env` missing a required key — check the compose file's required list.
2. Postgres unreachable — `docker compose logs postgres` if you run the
   fullstack overlay; else verify `POSTGRES_HOST` from the container's network.
3. Port already in use — change `LISTHOARD_PORT` (or the quickstart's mapped
   `8675`).

## Health endpoint

`GET /healthz` returns `{"status": "ok", "catalog_items": N}`; anything else is
the app telling you, not a guess.
