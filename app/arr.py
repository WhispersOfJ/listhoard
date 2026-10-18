"""arr + Jellyfin clients (docs/book-of-api anchors cited per call).

Radarr: docs/book-of-api/radarr.md — movie (GET/POST), movie/lookup/tmdb,
tags, movie/DELETE. Sonarr: docs/book-of-api/sonarr.md — series (GET/POST),
series/lookup, tags. Jellyfin: docs/book-of-api/jellyfin.md —
/Users/AuthenticateByName, POST /Collections, POST /Collections/{id}/Items,
POST /Library/Library/Refresh (BEWARE: the deployed server answers
/Library/Refresh, not the book's /Library/Library/Refresh — the literal path
wins, per the MCU spec's §7.6 correction and AGENTS §17 "source wins").
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Literal

import httpx

log = logging.getLogger("listhoard.arr")


def _base() -> dict[str, str]:
    return {
        "radarr": os.environ.get("RADARR_BASE_URL", "http://legends-radarr:7878"),
        "sonarr": os.environ.get("SONARR_BASE_URL", "http://legends-sonarr:8989"),
        "jellyfin": os.environ.get("JELLYFIN_BASE_URL", "http://legends-jellyfin:8096"),
    }


def _key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} missing from environment")
    return v


@dataclass
class _Cache:
    ttl: float = 60.0
    data: tuple[dict, float] | None = None

    def get(self) -> dict | None:
        if self.data and time.time() - self.data[1] < self.ttl:
            return self.data[0]
        return None

    def put(self, d: dict) -> dict:
        self.data = (d, time.time())
        return d


_lib_cache = {"radarr": _Cache(), "sonarr": _Cache()}
_res_cache: dict[str, _Cache] = {}
_res_lock = threading.Lock()


class ArrError(RuntimeError):
    pass


def _call(
    app: Literal["radarr", "sonarr", "jellyfin"],
    method: str,
    path: str,
    params: dict | None = None,
    json: dict | list | None = None,
    token: str | None = None,
) -> httpx.Response:
    headers = {}
    if app in ("radarr", "sonarr"):
        headers["X-Api-Key"] = _key(f"{app.upper()}_API_KEY")
    else:
        # Deployed Jellyfin 12 parses application identity from the
        # Authorization header; the legacy X-Emby-Authorization yields a null
        # App and a 400 (verified against the live server 2026-10-17). With a
        # token, identity and token combine in that one header.
        ident = 'Client="ListHoard", Device="ListHoard", '
        headers["Authorization"] = (
            f'MediaBrowser {ident}DeviceId="listhoard", Version="0.1.0"'
            + (f', Token="{token}"' if token else "")
        )
    base = _base()[app]
    with httpx.Client(timeout=30) as c:
        r = c.request(
            method,
            f"{base}{path}",
            params=params,
            json=json,
            headers=headers,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            raise ArrError(f"{app} {method} {path} -> {r.status_code}: {r.text[:200]}")
        return r


def library(app: Literal["radarr", "sonarr"]) -> dict[str, dict]:
    cache = _lib_cache[app]
    hit = cache.get()
    if hit is not None:
        return hit  # type: ignore[return-value]
    path = "/api/v3/movie" if app == "radarr" else "/api/v3/series"
    rows = _call(app, "GET", path).json()
    out: dict[str, dict] = {}
    idfield = "tmdbId" if app == "radarr" else "tvdbId"
    for row in rows:
        out[str(row.get(idfield))] = {
            "id": row["id"],
            "monitored": row.get("monitored", False),
            "tags": row.get("tags", []),
        }
    return cache.put(out)  # type: ignore[return-value]


def lookup(app: Literal["radarr", "sonarr"], item: dict) -> dict | None:
    """Search for a catalog item in the arr's own lookup; result cached.

    Cache criticisms: keyed by (app, mediatype+ids); TTL 12h because the
    catalog itself only changes on sync (§11.4).
    """
    ck = f"{app}:{item['mediatype']}:{item['tmdb_id'] or item['tvdb_id']}"
    with _res_lock:
        cache = _res_cache.setdefault(ck, _Cache(ttl=12 * 3600))
        hit = cache.get()
    if hit is not None:
        return hit or None  # None means "known unresolvable"
    try:
        if app == "radarr":
            params = {"tmdbId": item["tmdb_id"]}
            res = _call("radarr", "GET", "/api/v3/movie/lookup/tmdb", params).json()
            hit = res if res else {"_miss": True}
        else:
            if not item.get("tvdb_id") and not item.get("trakt_id"):
                with _res_lock:
                    cache.put({"_miss": True})
                return None
            term = (
                f"tvdb:{item.get('tvdb_id')}"
                if item.get("tvdb_id")
                else f"trakt:{item.get('trakt_id')}"
            )
            rows = _call(
                "sonarr", "GET", "/api/v3/series/lookup", {"term": term}
            ).json()
            hit = rows[0] if rows else {"_miss": True}
    except httpx.HTTPError as e:
        raise ArrError(f"lookup failed: {e}") from e
    with _res_lock:
        cache.put(hit)
    return None if hit.get("_miss") else hit


def root_folders(app: Literal["radarr", "sonarr"]) -> list[dict]:
    return _call(app, "GET", "/api/v3/rootfolder").json()


def ensure_root_folder(app: Literal["radarr", "sonarr"], path: str) -> None:
    for row in root_folders(app):
        if row.get("path") == path:
            return
    _call(app, "POST", "/api/v3/rootfolder", json={"path": path, "name": path})


def profiles(app: Literal["radarr", "sonarr"]) -> list[dict]:
    return _call(app, "GET", "/api/v3/qualityprofile").json()


def profile_choice(
    app: Literal["radarr", "sonarr"], anime: bool
) -> tuple[int, str | None]:
    """Pick the profile; the anime profile must exist through the CustomFormats
    pipeline (AGENTS §18). If it does not exist yet, degrade to the 2160p
    default and say so — never create it from here (A48/A49)."""
    rows = profiles(app)
    by_name = {r["name"]: r["id"] for r in rows}
    if app == "sonarr" and anime and "Anime 2160p" in by_name:
        return by_name["Anime 2160p"], None
    default = "Movies 2160p" if app == "radarr" else "TV 2160p"
    warn = (
        (
            "anime profile 'Anime 2160p' not present in Sonarr; falling back to "
            f"{default} (spec §12.2 — create it via arr-custom-formats.json)"
        )
        if (app == "sonarr" and anime)
        else None
    )
    return by_name[default], warn


def ensure_tag(app: Literal["radarr", "sonarr"], label: str) -> int:
    rows = _call(app, "GET", "/api/v3/tag").json()
    for r in rows:
        if r.get("label") == label:
            return r["id"]
    return int(_call(app, "POST", "/api/v3/tag", json={"label": label}).json()["id"])


def tagged_objects(app: Literal["radarr", "sonarr"], tag_label: str) -> list[dict]:
    lib = library(app)
    tags = {r["id"]: r["label"] for r in _call(app, "GET", "/api/v3/tag").json()}
    tid = next((i for i, lbl in tags.items() if lbl == tag_label), None)
    if tid is None:
        return []
    return [v for v in lib.values() if tid in v.get("tags", [])]


def delete_object(app: Literal["radarr", "sonarr"], arr_id: int) -> bool:
    """Delete one library entry WITHOUT deleting files. This implements the
    unimport flow (spec §12.5): `deleteFiles` defaults false on both arrs, so
    this removes only the library entry — never the produced media (B5)."""
    path = f"/api/v3/movie/{arr_id}" if app == "radarr" else f"/api/v3/series/{arr_id}"
    _call(app, "DELETE", path, params={"deleteFiles": "false"})
    return True


def add_movie(item: dict, opts: dict) -> int:
    res = lookup("radarr", item)
    if not res:
        raise ArrError("unresolvable in Radarr")
    body = dict(res)
    body.update(
        qualityProfileId=opts["profile_id"],
        rootFolderPath=opts["root_folder"],
        monitored=opts["monitor"],
        minimumAvailability=opts["minimum_availability"],
        tags=opts["tags"],
        addOptions={"searchForMovie": opts["search_on_add"]},
    )
    created = _call("radarr", "POST", "/api/v3/movie", json=body)
    # A successful first-time add answers 201 Created, not 200 — the response
    # is a 2xx create either way. (truth/radarr/upstream/src/Radarr.Api.V3/
    # Movies/MovieController.cs:243 `return Created(movie.Id)`.)
    if created.status_code // 100 != 2:
        raise ArrError(f"add failed: {created.status_code}")
    j = created.json()
    if not isinstance(j.get("id"), int):
        raise ArrError("add returned no id")
    return j["id"]


def add_series(item: dict, opts: dict) -> int:
    res = lookup("sonarr", item)
    if not res:
        raise ArrError("unresolvable in Sonarr")
    body = dict(res)
    body.update(
        qualityProfileId=opts["profile_id"],
        rootFolderPath=opts["root_folder"],
        monitored=True,
        seasonFolder=True,
        seriesType=opts.get("series_type", "standard"),
        monitorNewItems="all",
        tags=opts["tags"],
        addOptions={
            "monitor": "all",
            "searchForMissingEpisodes": opts["search_on_add"],
        },
    )
    for s in body.get("seasons") or []:
        s["monitored"] = True
    created = _call("sonarr", "POST", "/api/v3/series", json=body)
    # Same 2xx create contract as movies (truth/sonarr/upstream/src/
    # Sonarr.Api.V3/Series/SeriesController.cs:175 `return Created(series.Id)`).
    if created.status_code // 100 != 2:
        raise ArrError(f"add failed: {created.status_code}")
    j = created.json()
    if not isinstance(j.get("id"), int):
        raise ArrError("add returned no id")
    return j["id"]


# ---------------------------------------------------------------- Jellyfin ---


def authenticate_by_name(user: str, password: str) -> str:
    r = _call(
        "jellyfin",
        "POST",
        "/Users/AuthenticateByName",
        json={"Username": user, "Pw": password},
    )
    if r.status_code != 200:
        raise ArrError("jellyfin auth failed")
    return r.json().get("AccessToken", "")


def jellyfin_library_refresh(token: str) -> None:
    # Book says /Library/Library/Refresh; the deployed server answers 404 there
    # and 204 on /Library/Refresh. The literal path wins (AGENTS §17).
    _call("jellyfin", "POST", "/Library/Refresh", token=token).raise_for_status()


def jellyfin_find_collection(token: str, name: str) -> str | None:
    rows = _call(
        "jellyfin",
        "GET",
        "/Items",
        params={"includeItemTypes": "BoxSet", "searchTerm": name},
        token=token,
    ).json()
    for row in rows.get("Items", rows if isinstance(rows, list) else []):
        if row.get("Name") == name:
            return row["Id"]
    return None


def jellyfin_resolve_item_ids(token: str, items: list[dict]) -> list[str]:
    """Resolve library item ids for the freshly imported catalog items (the
    collection is built after the import completes, so these exist already)."""
    found: list[str] = []
    for it in items:
        pid = f"Tmdb.{it['tmdb_id']}" if it.get("tmdb_id") else f"Tvdb.{it['tvdb_id']}"
        rows = _call(
            "jellyfin",
            "GET",
            "/Items",
            params={"anyProviderIdEquals": pid, "recursive": "true", "limit": "1"},
            token=token,
        ).json()
        rows = rows.get("Items", rows if isinstance(rows, list) else [])
        if rows:
            found.append(rows[0]["Id"])
    return found


def jellyfin_collection_for_import(
    token: str, list_name: str, item_ids: list[str]
) -> str:
    name = list_name[:120]
    cid = jellyfin_find_collection(token, name)
    if cid is None:
        cid = (
            _call(
                "jellyfin",
                "POST",
                "/Collections",
                params={"Name": name},
                token=token,
            )
            .json()
            .get("Id")
        )
    if item_ids:
        _call(
            "jellyfin",
            "POST",
            f"/Collections/{cid}/Items",
            params={"ids": ",".join(item_ids)},
            token=token,
        )
    return cid


TAG_RX = re.compile(r"[a-z0-9-]+")
