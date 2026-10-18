"""Image cache (docs/specs/ListHoard.spec.md §10).

Local, uncapped, demand-populated. One canonical copy per (source id, kind);
the browser never asks a provider CDN directly.
Layout: /data/images/tmdb/<list-id>/<kind>/<tmdbid>.jpg
"""

import json
import logging
import os
import re
import threading

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

log = logging.getLogger("listhoard.images")

_root = os.environ.get("LISTHOARD_IMAGES_DIR", "/data/images")
_locks: dict[str, threading.Lock] = {}
_lock = threading.Lock()

_SAFE = re.compile(r"^[a-zA-Z0-9/_.-]+$")  # tmdb hashes carry uppercase and .jpg
_TMDB = "https://image.tmdb.org/t/p"


def thumb_path(poster_path: str, tmdb_id: int, kind: str = "posters") -> str | None:
    """The cached path for a poster given the item record's columns."""
    if not poster_path:
        return None
    rel = f"tmdb/{kind}/{tmdb_id}{poster_path}"
    abs_path = os.path.join(_root, rel)
    if os.path.exists(abs_path):
        return _serve_rel(rel)
    lock = _locks.setdefault(rel, threading.Lock())
    with lock:
        if not os.path.exists(abs_path):
            os.makedirs(os.path.dirname(abs_path), mode=0o750, exist_ok=True)
            url = f"{_TMDB}/w342{poster_path}"
            try:
                with httpx.Client(timeout=20) as c:
                    r = c.get(url)
                r.raise_for_status()
                tmp = abs_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, abs_path)
            except httpx.HTTPError as e:
                log.warning("poster fetch %s failed: %s", poster_path, e)
                return None
    return _serve_rel(rel)


def _serve_rel(rel: str) -> str:
    return f"/img/{rel}"


def resolve_poster(tmdb_id: int, tvdb_id: int | None = None) -> str | None:
    """Demand-populate the poster for an item that carries ids but no cached
    poster path (Trakt/MDBList rows). Movies resolve by tmdb id; shows resolve
    by tvdb id through TMDB's /find. Shares thumb_path's canonical on-disk
    file, and records the path in the catalog so later renders skip the
    lookup. None when TMDB has no poster for it."""
    try:
        import urllib.parse
        import urllib.request

        key = os.environ["TMDB_API_KEY"]
        file_key: int
        if tmdb_id:
            url = (
                "https://api.themoviedb.org/3/movie/"
                + str(tmdb_id)
                + "?"
                + urllib.parse.urlencode({"api_key": key})
            )
            with urllib.request.urlopen(url, timeout=20) as r:
                src = json.load(r)
            poster_path = src.get("poster_path")
            file_key = tmdb_id
        elif tvdb_id:
            url = (
                "https://api.themoviedb.org/3/find/"
                + str(tvdb_id)
                + "?"
                + urllib.parse.urlencode({"api_key": key, "external_source": "tvdb_id"})
            )
            with urllib.request.urlopen(url, timeout=20) as r:
                src = json.load(r)
            tv = (src.get("tv_results") or [None])[0]
            if tv is None:
                return None
            poster_path = tv.get("poster_path")
            file_key = tv.get("id") or tvdb_id  # the found TMDB id dedupes
        else:
            return None
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError) as e:
        log.warning("poster resolve failed: %s", e)
        return None
    if not poster_path:
        return None
    saved = thumb_path(poster_path, file_key)
    if saved is not None:
        try:
            from . import db

            if tmdb_id:
                cur_sql = (
                    "UPDATE items SET poster_path = %s"
                    " WHERE tmdb_id = %s AND poster_path IS NULL"
                )
                cur_params = (poster_path, tmdb_id)
            else:
                cur_sql = (
                    "UPDATE items SET poster_path = %s"
                    " WHERE tvdb_id = %s AND poster_path IS NULL"
                )
                cur_params = (poster_path, tvdb_id)
            with db.cursor() as cur:
                cur.execute(cur_sql, cur_params)
        except Exception:
            log.exception("poster column update failed")
    return saved


def serve(rel: str) -> FileResponse:
    if not _SAFE.match(rel) or ".." in rel:
        raise HTTPException(status_code=400, detail="bad image path")
    fs_path = os.path.realpath(os.path.join(_root, rel))
    if not fs_path.startswith(os.path.realpath(_root) + os.sep):
        raise HTTPException(status_code=400, detail="bad image path")
    if not os.path.exists(fs_path):
        raise HTTPException(status_code=404)
    # Cached images are content-stable (one canonical copy per id+kind), so a
    # browser can keep them for weeks; a re-sync that swaps a file bumps the
    # URL? No — it overwrites in place, hence a day, not immutable.
    return FileResponse(fs_path, headers={"Cache-Control": "public, max-age=86400"})
