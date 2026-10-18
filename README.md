# ListHoard

Self-hosted browser and one-click importer for public movie/TV lists. ListHoard
syncs the lists of seven providers into one local, deduplicated catalog, caches
every poster on disk (the browser never touches a provider CDN), and imports any
browsed list into Radarr or Sonarr with the arrs' full add-options surface —
root folder, quality profile, monitoring, tags, search-on-add — with per-item
curation and live "already in your library" badges.

```
 TMDB ─┐                                        ┌─→ Radarr  (movies)
 Trakt ─┤                                       │
 MDBList ─┤   ┌────────────┐   ┌───────────┐   │
 Letterboxd ─┼─>│  Sync      │──>│ Postgres  │──>│─┼─→ Sonarr  (shows)
 IMDb       │  │  (daily)   │   │  catalog  │   │ │
 RT         │  └────────────┘   └───────────┘   └─┼─→ Jellyfin (collections)
 TVDB ─┤         │
             ┌────▼────────────────┐
             │ Image cache (local) │
             └─────────────────────┘
```

- **Seven providers at launch** — TMDB, Trakt, MDBList, Letterboxd, IMDb,
  Rotten Tomatoes, TVDB (plus an opt-in explicit-content module, see below).
- **Polite sync** — per-provider request budgets (hour + day windows) pause a
  source instead of erroring it; the health board shows used/limit per source.
- **Curation is law** — an import is always the rows you explicitly checked;
  nothing is ever imported wholesale without a selection.
- **Self-hosted, single-house** — one server, one postgres, your LAN.

## Quickstart (ten minutes)

You need Docker and exactly one provider key: a free
[TMDB](https://www.themoviedb.org/signup) key. Your arrs are optional (the
hoard browses happily without them; imports need them).

```bash
git clone https://github.com/WhispersOfJ/listhoard
cd listhoard
cp .env.example .env
$EDITOR .env        # fill TMDB_API_KEY + your postgres and arr values
docker compose up -d
# arr-less? run postgres for me too:
docker compose -f docker-compose.yml -f docker-compose.fullstack.yml up -d
```

Open `http://localhost:8675` → the dashboard. Your catalog is **empty by
design**: paste any provider's list page into *add-list by URL* and the first
sync fills it — TMDB, Trakt, MDBList, Letterboxd, IMDb, Rotten-Tomatoes and
TVDB pages all resolve.

## Configuration

Every key lives in `.env` (copy `.env.example`; real secrets never go in a
committed file). The full reference including the budget knobs, auth modes and
the explicit-content gate is in [docs/env.md](docs/env.md).

## Docs

Built with mkdocs — `pip install mkdocs-material && mkdocs serve`:

- [Providers](docs/providers.md) — which sources exist, which read an API and
  which scrape, and what each one needs
- [Environment reference](docs/env.md) — every `.env` key
- [Troubleshooting](docs/troubleshooting.md)
- [Upgrading](docs/upgrading.md) — semver discipline for the app

## License

[Apache-2.0](LICENSE).
