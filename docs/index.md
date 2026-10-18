# ListHoard

Self-hosted browser and one-click importer for public movie/TV lists. Seven
providers sync daily into one local, deduplicates catalog; every poster is
cached on disk; any browsed list imports into Radarr or Sonarr with the arrs'
full add-options surface; every import can be un-imported.

## Highlights

- **Empty by design** — the app ships with no bundled lists; you paste a list
  URL and it syncs from there.
- **Polite sync** — per-provider hour/day request budgets; a throttled source
  *pauses* and shows used/limit on the health board, rather than erroring.
- **Curation first** — imports are the rows you checked, never a bulk grab.
- **Anime is first-class** — anime-flagged lists import into Sonarr with
  `seriesType = anime`.
- **Explicit content stays off** — the adult module does not even register
  unless you set `LISTHOARD_ENABLE_XXX=on` yourself.

## Screenshots

The abyss theme renders dark, poster-forward pages. Placeholder — captures of
the dashboard, browse, list detail and import dialog land here with the first
1.x cut.

## Where to go next

- [Providers](providers.md) — the seven sources and their key needs
- [Configuration](env.md) — every `.env` key
- The ten-minute quickstart lives in the project [README](https://github.com/WhispersOfJ/listhoard#quickstart-ten-minutes)
