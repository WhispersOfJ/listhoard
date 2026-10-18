# Upgrading

The app follows semver: `0.x` is the shippability ramp; `1.0.0` marks public
readiness. Releases land as [GitHub releases](https://github.com/WhispersOfJ/listhoard/releases)
with per-release notes.

## Update

```bash
git pull
docker compose build && docker compose up -d
# fullstack overlay:
docker compose -f docker-compose.yml -f docker-compose.fullstack.yml build
docker compose -f docker-compose.yml -f docker-compose.fullstack.yml up -d
```

## What carries over

- Your catalog and import history live in postgres — for the fullstack overlay
  that is the `pg-data` named volume; keep it backed up if import history
  matters to you (the catalog rebuilds from the sources; the import history
  does not).
- The image cache lives in the `listhoard-images` volume — nothing to migrate,
  it repopulates on demand if ever lost.

## Breaking changes

A `0.x` bump that changes a `.env` key's meaning or a compose shape calls it
out in the release notes and in `CHANGELOG.md`. Check the notes before
`git pull` when you have a long-lived deployment; the app version at any
commit is in the release tag.
