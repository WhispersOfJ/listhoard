"""Import engine (the ListHoard specification §12).

Background jobs, strictly sequential (the MCU-run precedent): one add, read
its response, then the next. Already-present rows skip unless forced;
unresolvable rows log and move on. On completion, one Jellyfin library
refresh and the Jellyfin collection are attempted, best effort, and never
abort the job.
"""

import logging
import threading
import time

from . import arr, db

log = logging.getLogger("listhoard.importer")

_jobs: dict[int, dict] = {}
_finished: list[dict] = []  # last completions, for the UI toast/job surfaces
_lock = threading.Lock()


def _job(import_id: int) -> dict:
    with _lock:
        return _jobs[import_id]


def running():
    """Live job state for the UI surfaces (ListHoard-UX.spec.md §5.4):
    running jobs with progress counters, plus recent completions."""
    with _lock:
        running = [
            {
                "import_id": jid,
                "list_id": st["list_id"],
                "app": st["app"],
                "total": st["total"],
                "added": st["added"],
                "skipped": st["skipped"],
                "failed": st["failed"],
                "elapsed": round(time.time() - st["started"], 1),
            }
            for jid, st in _jobs.items()
        ]
        done = list(_finished)
    return running, done


def _tally(import_id: int, **delta: int) -> None:
    """Live progress counters for the UI surfaces; never throws into the run."""
    with _lock:
        st = _jobs.get(import_id)
        if st is not None:
            for k, v in delta.items():
                st[k] = st.get(k, 0) + v


def running_for_list(list_id: int) -> int | None:
    """The import id of a job currently running for one list, if any."""
    with _lock:
        for jid, st in _jobs.items():
            if st["list_id"] == list_id:
                return jid
    return None


def start_reissue(import_id: int) -> dict:
    """Re-issue the failed rows of one past import as a fresh job (spec
    ListHoard-UX §5.4 'arr re-issue affordances'). The stored options are
    replayed verbatim; nothing runs un-invoked — a route calls this."""
    opts = db.import_options(import_id)
    if opts is None:
        raise RuntimeError(f"import {import_id} not found")
    item_ids = db.failed_rows(import_id)
    if not item_ids:
        return {"import_id": None, "retested": 0, "app": opts["app"]}
    list_id = opts["list_id"]
    options = {
        "tag_label": opts["tag_label"],
        "root_folder": opts["root_folder"],
        "profile_id": opts["profile_id"],
        "monitor": opts["monitor"],
        "minimum_availability": opts["minimum_availability"],
        "search_on_add": opts["search_on_add"],
        "force": opts["force"],
        "series_type": opts["series_type"],
    }
    res = start_import(list_id, item_ids, opts["app"], options)
    res["retested"] = len(item_ids)
    return res


def start_import(
    list_id: int, item_ids: list[int], app_name: str, options: dict
) -> dict:
    """Validate options, create the tag, spawn the worker, return job state."""
    tag_id = arr.ensure_tag(app_name, options["tag_label"])
    opts = dict(options)
    opts["tags"] = [tag_id]
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO imports (list_id, app, tag_label, root_folder, profile_id,
                                 series_type, search_on_add, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'running') RETURNING id
            """,
            (
                list_id,
                app_name,
                options["tag_label"],
                options["root_folder"],
                options["profile_id"],
                options.get("series_type"),
                options["search_on_add"],
            ),
        )
        import_id = int(cur.fetchone()[0])

    def run() -> None:
        added = failed = skipped = 0
        try:
            for iid in item_ids:
                with db.cursor() as cur:
                    cur.execute(
                        """SELECT id, title, mediatype, tmdb_id, tvdb_id
                            FROM items WHERE id = %s""",
                        (iid,),
                    )
                    row = cur.fetchone()
                if row is None:
                    continue
                item = {
                    "id": row[0],
                    "title": row[1],
                    "mediatype": row[2],
                    "tmdb_id": row[3],
                    "tvdb_id": row[4],
                }
                if (
                    item["mediatype"] == "show"
                    if app_name == "sonarr"
                    else item["mediatype"] == "movie"
                ):
                    pass
                try:
                    wants_movie = app_name == "radarr"
                    if (item["mediatype"] == "movie") != wants_movie:
                        # A mixed list imports to one app by choice; rows of
                        # the other medium are skipped with a reason.
                        status, detail, nid = (
                            "skipped-unresolvable",
                            "item is not this app's medium",
                            None,
                        )
                    elif app_name == "radarr":
                        lib = arr.library("radarr")
                        key = str(item["tmdb_id"] or "")
                        if key in lib and not opts.get("force"):
                            status, detail, nid = (
                                "skipped-present",
                                f"arr id {lib[key]['id']}",
                                None,
                            )
                        else:
                            nid = arr.add_movie(item, opts)
                            status, detail = "added", None
                    else:
                        lib = arr.library("sonarr")
                        key = str(item["tvdb_id"] or "")
                        if key in lib and not opts.get("force"):
                            status, detail, nid = (
                                "skipped-present",
                                f"arr id {lib[key]['id']}",
                                None,
                            )
                        else:
                            nid = arr.add_series(item, opts)
                            status, detail = "added", None
                    if status == "added":
                        added += 1
                    else:
                        skipped += 1
                    _tally(
                        import_id,
                        **{"added": 1} if status == "added" else {"skipped": 1},
                    )
                    with db.cursor() as cur:
                        cur.execute(
                            """INSERT INTO import_items
                               (import_id, item_id, arr_id, status, detail)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (import_id, item["id"], nid, status, detail),
                        )
                except Exception as e:  # noqa: BLE001 — record + continue (§12)
                    failed += 1
                    _tally(import_id, failed=1)
                    log.warning("import %s item %s failed: %s", import_id, iid, e)
                    with db.cursor() as cur:
                        cur.execute(
                            """INSERT INTO import_items
                               (import_id, item_id, status, detail)
                               VALUES (%s, %s, 'failed', %s)""",
                            (import_id, iid, str(e)[:400]),
                        )
        finally:
            with db.cursor() as cur:
                cur.execute(
                    """UPDATE imports SET status = 'done', added = %s,
                        failed = %s WHERE id = %s""",
                    (added, failed, import_id),
                )
            st = _jobs.pop(import_id, None)
            with _lock:
                _finished.insert(
                    0,
                    {
                        "import_id": import_id,
                        "list_id": list_id,
                        "app": app_name,
                        "added": added,
                        "skipped": st.get("skipped", 0) if st else 0,
                        "failed": failed,
                        "at": time.time(),
                    },
                )
                del _finished[20:]
        # Post-import: refresh + collection (§12.4), best effort, never aborts.
        _post_import(import_id, options, list_id)

    t = threading.Thread(target=run, name=f"import-{import_id}", daemon=True)
    with _lock:
        _jobs[import_id] = {
            "list_id": list_id,
            "app": app_name,
            "total": len(item_ids),
            "added": 0,
            "skipped": 0,
            "failed": 0,
            "started": time.time(),
        }
    t.start()
    return {"import_id": import_id, "app": app_name, "tag": options["tag_label"]}


def _post_import(import_id: int, options: dict, list_id: int) -> None:
    import os

    jf_user = os.environ.get("JELLYFIN_ADMIN_USER")
    jf_pass = os.environ.get("JELLYFIN_ADMIN_PASSWORD")
    try:
        if not (jf_user and jf_pass):
            log.warning(
                "no JELLYFIN admin creds; refresh/collection skipped for %s", import_id
            )
            return
        jf_session_token_value = arr.authenticate_by_name(jf_user, jf_pass)
    except Exception as e:  # noqa: BLE001 — post-import is best effort (§12.4)
        log.warning("jellyfin auth for import %s failed: %s", import_id, e)
        return
    try:
        arr.jellyfin_library_refresh(jf_session_token_value)
        with db.cursor() as cur:
            cur.execute(
                """SELECT i.tmdb_id, i.tvdb_id FROM import_items ii
                    JOIN items i ON i.id = ii.item_id
                    WHERE ii.import_id = %s AND ii.status = 'added' LIMIT 200""",
                (import_id,),
            )
            rows = cur.fetchall()
            cur.execute("SELECT name FROM lists WHERE id = %s", (list_id,))
            list_name = cur.fetchone()[0]
        ids = arr.jellyfin_resolve_item_ids(
            jf_session_token_value,
            [{"tmdb_id": r[0], "tvdb_id": r[1]} for r in rows],
        )
        arr.jellyfin_collection_for_import(jf_session_token_value, list_name, ids)
    except Exception as e:  # noqa: BLE001
        log.warning("jellyfin collection for import %s failed: %s", import_id, e)


def start_unimport(import_id: int) -> threading.Thread:
    """Unimport: resolve the per-import tag's objects and remove them, exactly
    the destructive-gated path of spec §12.5 — each entry is shown first by the
    caller (the confirm route), never a silent bulk delete."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT app, tag_label FROM imports WHERE id = %s""",
            (import_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"import {import_id} not found")
        app_name, tag_label = row

    def run() -> None:
        objs = arr.tagged_objects(app_name, tag_label)
        for o in objs:
            log.info(
                "unimport %s: removing arr id %s in %s", import_id, o["id"], app_name
            )
            arr.delete_object(app_name, o["id"])
        with db.cursor() as cur:
            cur.execute("DELETE FROM import_items WHERE import_id = %s", (import_id,))
            cur.execute(
                "UPDATE imports SET status = 'unimported', added = 0 WHERE id = %s",
                (import_id,),
            )

    t = threading.Thread(target=run, name=f"unimport-{import_id}", daemon=True)
    t.start()
    return t
