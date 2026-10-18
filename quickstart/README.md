# ListHoard quickstart

Ten contraband minutes to a running hoard (Shippability spec §12, slice 1).

1. `cp .env.example .env` — fill **`TMDB_API_KEY`** (free signup at
   [themoviedb.org](https://www.themoviedb.org/signup)), your postgres values,
   and your arr URLs + keys.
2. `docker compose up -d` (with an existing postgres) or
   `docker compose -f docker-compose.yml -f docker-compose.fullstack.yml up -d`
   (embedded postgres, arr-less).
3. Open `http://localhost:8675` → the dashboard. Your catalog is empty by
   design — paste any provider's list page into **add-list by URL**, and the
   first sync fills it: `TMDB/Trakt/MDBList/Letterboxd/IMDb/Rotten-Tomatoes/TVDB`
   pages all resolve.

What ships **off** unless you turn it on: explicit content (XXX) sources
(`LISTHOARD_ENABLE_XXX=on` to opt in), and any preloaded lists (there are none
— the hoard carries only what you chose; Shippability ruling 8).

Currently the image builds from this repo's source (`build: ..`). When the
public registry publishing lands (spec T2), this file swaps to a pinned
`image:` — no other change.
