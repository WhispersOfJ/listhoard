"""ListHoard FastAPI app (the ListHoard specification §6.2).

Server-rendered Jinja + plain forms, no SPA toolchain. Auth matches the
Jellyfin account model (ruling 21): every non-/healthz, non-/img, non-/login
route requires a session obtained from POST /login, which authenticates
against the Jellyfin server (Jellyfin's /Users/AuthenticateByName).

Refactor note (the optimization specification §7.4): routes shape requests;
the data work lives in db.py. Cache-key namespaces and response semantics are
byte-identical to the pre-refactor probes recorded in
docs/optimization-evidence.md.
"""

import hashlib
import logging
import math
import os
import threading
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import adapters, arr, auth, budget, db, discover, images, importer, seed, sync

log = logging.getLogger("listhoard.main")
app = FastAPI(title="ListHoard", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- steady-state metrics (the optimization specification §7.5)  -----------

# Embedded at module level so the threshold is bound before the middleware
# registers. Env-overridable; defaults tuned in Batch-2c from the profile.
_SLOW_REQUEST_MS = float(os.environ.get("LISTHOARD_SLOW_REQUEST_MS", "500"))

PAGE = 25


@app.middleware("http")
async def _slow_request_log(request: Request, call_next):
    """Log every request above the slow threshold. Additive: it never alters
    response bytes (verified: the Batch-2a probe hashes still hold)."""
    start = time.perf_counter()
    resp = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if elapsed_ms >= _SLOW_REQUEST_MS:
        log.warning(
            "slow request: %s %s -> %d in %.0f ms",
            request.method,
            request.url.path,
            resp.status_code,
            elapsed_ms,
        )
    return resp


# --- caching foundations (ListHoard-UX.spec.md §4) -------------------------

_DATA_CACHE: dict[str, tuple[float, object]] = {}
_DATA_TTL = 20.0
_WRITE_GEN = 0
_gen_lock = threading.Lock()


def bust_cache() -> None:
    """Invalidate the server query cache and make rolling ETags lag by a
    beat. Called by every POST that mutates hoard or arr state."""
    global _WRITE_GEN
    with _gen_lock:
        _WRITE_GEN += 1
    _DATA_CACHE.clear()


def _cached(key: str, fn):
    """Tiny TTL memo for the catalog's expensive read queries. The generation
    counter busts it the moment any POST mutates state."""
    gen = _WRITE_GEN
    hit = _DATA_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _DATA_TTL and hit[2] == gen:
        return hit[1]
    val = fn()
    _DATA_CACHE[key] = (now, val, gen)
    return val


@app.middleware("http")
async def _etag(request: Request, call_next):
    """Rolling ETag on every GET response; a matching If-None-Match serves
    304 with an empty body (ListHoard-UX.spec.md §4). Streaming responses
    (TemplateResponse, FileResponse) are buffered for hashing; /img streams
    through untouched because it carries its own Cache-Control headers."""
    if request.method not in ("GET", "HEAD") or request.url.path.startswith("/img"):
        return await call_next(request)
    resp = await call_next(request)
    body = getattr(resp, "body", None)
    if body is None:
        chunks = [chunk async for chunk in resp.body_iterator]
        body = b"".join(chunks)
        resp = Response(
            content=body,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            media_type=resp.media_type,
        )
    if body:
        tag = '"' + hashlib.md5(bytes(body)).hexdigest()[:24] + '"'
        resp.headers["ETag"] = f"W/{tag}"
        # Clients echo W/"tag"; compare on the bare token as well.
        inm = request.headers.get("if-none-match", "").replace("W/", "").strip()
        if inm == tag:
            resp.status_code = 304
            resp.body = b""
    return resp


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO)
    db.init_schema()
    if seed.seed_on_boot():
        counts = seed.load_seed()
        log.info("seed loaded: %s", counts)
    else:
        # Shippability ruling 8: a fresh instance ships an empty catalog;
        # source slugs register, the research seed stays opt-in.
        seed.register_sources()
        log.info("seed skipped (LISTHOARD_SEED_ON_BOOT off); catalog empty")
    # Local accounts: the env-bootstrapped admin (Shippability §7). The hash
    # is noted in the log; the password itself is never echoed.
    if seed.seed_on_boot() or auth.resolved_mode() == "local":
        local_user = os.environ.get("LISTHOARD_LOCAL_ADMIN_USER")
        local_pass = os.environ.get("LISTHOARD_LOCAL_ADMIN_PASSWORD")
        if local_user and local_pass and auth.resolved_mode() == "local":
            db.upsert_account(local_user, auth.hash_password(local_pass))
            log.info("local auth bootstrapped for user %r", local_user)
        elif auth.resolved_mode() == "local":
            log.error("local auth mode set but LISTHOARD_LOCAL_ADMIN_* missing")
    sync.start_daily()


@app.on_event("shutdown")
def shutdown() -> None:
    sync.shutdown()


@app.get("/healthz")
def healthz() -> JSONResponse:
    counts = db.catalog_totals()
    return JSONResponse(
        {
            "status": "ok",
            "catalog_items": counts["items"],
            "lists_synced": counts["synced"],
        }
    )


def _require(request: Request) -> dict:
    """The session gate: one postgres-backed row (Shippability §7) or raise.
    Returns the row so mode-sensitive routes can read it. The content toggle
    check stays transparent to callers."""
    row = db.valid_session(request.cookies.get("lh_session"))
    if row is None:
        raise _Unauthed()
    if (
        request.query_params.get("xxx") in ("on", "1")
        or request.cookies.get("lh_toggle") == "xxx"
    ):
        pass  # content toggle: enables visibility below
    return row


class _Unauthed(Exception):
    pass


@app.exception_handler(_Unauthed)
def _unauthed(request: Request, exc: _Unauthed) -> RedirectResponse:
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": request.query_params.get("err"),
            # Shippability ruling 4: name what is missing, never a blank page.
            "auth_unset": auth.resolved_mode() is None,
        },
    )


@app.post("/login")
def login(
    request: Request, user: str = Form(...), password: str = Form(...)
) -> RedirectResponse:
    mode = auth.resolved_mode()
    if mode is None:
        # Unconfigured: no Jellyfin, no local accounts, no token. The setup
        # wizard (a later slice) owns this; today the route names it.
        return RedirectResponse("/login?err=2", status_code=302)
    account_id: int | None = None
    if mode == "jellyfin":
        try:
            arr.authenticate_by_name(user, password)
        except Exception:  # noqa: BLE001 — any failure is "bad credentials"
            return RedirectResponse("/login?err=1", status_code=302)
    elif mode == "local":
        acct = db.local_account(user)
        if acct is None or not auth.verify_password(password, acct["password_hash"]):
            return RedirectResponse("/login?err=1", status_code=302)
        account_id = acct["id"]
    else:  # token mode: any username, the shared secret in the password field
        if not auth.token_matches(password):
            return RedirectResponse("/login?err=1", status_code=302)
    tok = db.create_session(mode, account_id)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("lh_session", tok, max_age=7 * 86400, httponly=True)
    return resp


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    db.drop_session(request.cookies.get("lh_session"))
    resp = RedirectResponse("/login", status_code=302)
    # starlette's method is delete_cookie (del_cookie never existed — the
    # pre-shippability logout 500'd on every call; surfaced by the E2E).
    resp.delete_cookie("lh_session")
    return resp


@app.get("/")
def dashboard(request: Request) -> HTMLResponse:
    _require(request)
    totals = db.catalog_totals()
    recent = db.recent_imports()
    sources = []
    for slug in sorted(totals["per_source"]):
        row = db.source_health_row(slug)
        sources.append(
            {
                "slug": slug,
                "lists": totals["per_source"][slug],
                "last_ok_at": row[0] if row else None,
                "last_error": row[1] if row else None,
                "quarantined": adapters.quarantined(slug),
                "budget": budget.REGISTRY.get(slug).state(),
            }
        )
    running, _done = importer.running()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "totals": totals,
            "sources": sources,
            "recent": recent,
            "running_jobs": running,
        },
    )


@app.get("/health-board")
def health_board(request: Request) -> JSONResponse:
    _require(request)
    totals = db.catalog_totals()
    out = {}
    for slug in sorted(totals["per_source"]):
        out[slug] = {
            # The JSON edge lives in db.source_health_row_json: datetimes
            # arrive as ISO-8601 strings for starlette.JSONResponse. Raw
            # datetimes 500'd the route since it shipped
            # (tests/listhoard_healthboard_unit.py).
            "health": db.source_health_row_json(slug, with_delta=True),
            "quarantined": adapters.quarantined(slug),
            # Provider budget view (Shippability §8): used/limit per window.
            "budget": budget.REGISTRY.get(slug).state(),
        }
    return JSONResponse(out)


@app.get("/browse")
def browse(
    request: Request,
    page: int = 1,
    source: str | None = None,
    mediatype: str | None = None,
    anime: bool = False,
    toggle: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    _require(request)
    if toggle is not None:
        show_xxx = toggle == "xxx"
        resp = templates.TemplateResponse(
            request,
            "browse.html",
            {},
            headers={"Set-Cookie": f"lh_toggle={'xxx' if show_xxx else 'safe'}"},
        )
        return resp
    show_xxx = request.cookies.get("lh_toggle") == "xxx"
    rows, total = _cached(
        f"browse:{page}:{source}:{mediatype}:{anime}:{show_xxx}:{q}",
        lambda: db.list_page(
            (page - 1) * PAGE, PAGE, source, mediatype, anime, show_xxx, q
        ),
    )
    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": max(1, math.ceil(total / PAGE)),
            "source": source,
            "mediatype": mediatype,
            "anime": anime,
            "show_xxx": show_xxx,
            "q": q,
            "sources": sorted(db.catalog_totals()["per_source"]),
        },
    )


@app.get("/lists/{list_id}")
def list_detail(request: Request, list_id: int, page: int = 1) -> HTMLResponse:
    _require(request)
    lst = db.list_row(list_id)
    if lst is None:
        return templates.TemplateResponse(
            request, "list.html", {"missing": True, "list_id": list_id}
        )
    items, total = db.list_items(list_id, (page - 1) * PAGE, PAGE)
    lib = arr.library("radarr" if lst["mediatype"] != "show" else "sonarr")
    idf = "tmdb_id" if lst["mediatype"] != "show" else "tvdb_id"
    for it in items:
        k = str(it[idf] or "")
        it["in_library"] = k in lib
        it["monitored"] = lib.get(k, {}).get("monitored") if k in lib else False
        # Demand-populated image cache (spec §10): rows with a tmdb id but no
        # cached poster resolve it from TMDB exactly once.
        it["poster_url"] = None
        if it.get("poster_path") and it.get("tmdb_id"):
            it["poster_url"] = images.thumb_path(it["poster_path"], it["tmdb_id"])
        if it["poster_url"] is None:
            it["poster_url"] = images.resolve_poster(it["tmdb_id"], it["tvdb_id"])
        it["backdrop_url"] = (
            images.thumb_path(it["backdrop_path"], it["tmdb_id"], kind="backdrops")
            if it.get("backdrop_path") and it.get("tmdb_id")
            else None
        )
    profile_root = arr.profiles("radarr" if lst["mediatype"] != "show" else "sonarr")
    folders = arr.root_folders("radarr" if lst["mediatype"] != "show" else "sonarr")
    arr_ok = bool(profile_root and folders)
    threshold = int(os.environ.get("LISTHOARD_HUGE_LIST_THRESHOLD", "5000"))
    default_search = "false" if lst["item_count"] > threshold else "true"
    running_id = importer.running_for_list(list_id)
    prior = db.prior_imports(list_id)
    latest = prior[0] if running_id is None and prior else None
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "l": lst,
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, math.ceil(total / PAGE)),
            "profiles": profile_root,
            "folders": folders,
            "default_search": default_search,
            "app": "radarr" if lst["mediatype"] == "movie" else "sonarr",
            "running_import": running_id,
            "latest_import": latest,
            "prior_imports": prior,
            "arr_ok": arr_ok,
        },
    )


@app.post("/lists/{list_id}/import")
def list_import(
    request: Request,
    list_id: int,
    item_ids: str = Form(...),
    quality_profile: int = Form(...),
    root_folder: str = Form(...),
    monitor: bool = Form(True),
    minimum_availability: str = Form("released"),
    search_on_add: str = Form("true"),
    force: bool = Form(False),
    series_type: str = Form("standard"),
    target_app: str = Form("radarr"),
    tag_label: str | None = Form(None),
) -> RedirectResponse:
    _require(request)
    ids = sorted({int(x) for x in item_ids.split(",") if x})
    row = db.list_brief(list_id)
    if row["mediatype"] == "movie":
        app_name = "radarr"
    elif row["mediatype"] == "show":
        app_name = "sonarr"
    else:  # a mixed list imports into the app the user picks for these rows
        app_name = "sonarr" if target_app == "sonarr" else "radarr"
    profile_id, _warn = arr.profile_choice(app_name, row["anime"])
    if row["xxx"]:  # XXX list → the dedicated adult root (§12.2, ruling 18)
        arr.ensure_root_folder(app_name, "/media/adult")
        root_folder = "/media/adult"
    tag = (
        tag_label
        or adapters.slugify(row["name"])
        or f"{row['slug']}-{row['external_id'][:30]}"
    )
    opts = {
        "mediatype_checks": row["mediatype"],
        "tag_label": tag,
        "root_folder": root_folder,
        "profile_id": profile_id,
        "monitor": bool(monitor),
        "minimum_availability": minimum_availability,
        "search_on_add": search_on_add == "true",
        "force": bool(force),
        "series_type": series_type,
    }
    res = importer.start_import(list_id, ids, app_name, opts)
    log.info(
        "import %s started: %d items -> %s", res["import_id"], len(ids), res["app"]
    )
    bust_cache()
    from fastapi.responses import RedirectResponse as RR

    return RR(f"/lists/{list_id}?imported={res['import_id']}", status_code=302)


@app.get("/imports/{import_id}")
def import_status(request: Request, import_id: int) -> HTMLResponse:
    _require(request)
    j = db.import_row(import_id)
    if j is None:
        return templates.TemplateResponse(request, "import.html", {"missing": True})
    buckets = db.import_buckets(import_id)
    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "j": j,
            "buckets": buckets,
            "running": importer._jobs.get(import_id) is not None,
        },
    )


@app.get("/lists/{list_id}/confirm-unimport/{import_id}")
def confirm_unimport(request: Request, list_id: int, import_id: int) -> HTMLResponse:
    _require(request)
    row = db.import_simple(import_id)
    app_name, tag_label, status = row["app"], row["tag_label"], row["status"]
    objs = arr.tagged_objects(app_name, tag_label)
    return templates.TemplateResponse(
        request,
        "confirm_unimport.html",
        {
            "import_id": import_id,
            "list_id": list_id,
            "app": app_name,
            "tag_label": tag_label,
            "objs": objs,
            "status": status,
        },
    )


@app.post("/lists/{list_id}/unimport/{import_id}")
def do_unimport(request: Request, list_id: int, import_id: int) -> RedirectResponse:
    _require(request)
    importer.start_unimport(import_id)
    bust_cache()
    return RedirectResponse(f"/lists/{list_id}", status_code=302)


@app.post("/sources/{slug}/sync")
def source_sync(request: Request, slug: str) -> RedirectResponse:
    _require(request)
    sync.trigger_source(slug)
    bust_cache()
    return RedirectResponse("/", status_code=302)


@app.post("/sources/{slug}/discover")
def source_discover(request: Request, slug: str) -> RedirectResponse:
    "Ruling 14: the discovery crawler — registered lists, never auto-imports."
    _require(request)
    discover.trigger_discover(slug)
    bust_cache()
    return RedirectResponse("/", status_code=302)


@app.get("/add-list", response_class=HTMLResponse)
def add_list_form(request: Request) -> HTMLResponse:
    _require(request)
    return templates.TemplateResponse(
        request, "add_list.html", {"result": None, "url": ""}
    )


@app.post("/add-list")
def add_list(request: Request, url: str = Form(...)) -> HTMLResponse:
    """Ruling 14: paste a provider list page URL; the adapter's own identity
    resolves it and one sync is queued. Import still needs curation."""
    _require(request)
    result = discover.add_list_by_url(url)
    if result["ok"] and result["created"]:
        bust_cache()
    return templates.TemplateResponse(
        request, "add_list.html", {"result": result, "url": url}
    )


@app.get("/img/{path:path}")
def img(path: str):
    return images.serve(path)


# --- UX-layer endpoints (ListHoard-UX.spec.md §5.3, §5.4) ------------------


def _json_err(msg: str, code: int) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


@app.get("/jobs/poll")
def jobs_poll(request: Request) -> JSONResponse:
    """The UI's single heartbeat: running jobs with live counters plus the
    completions the toasts have not yet seen. Polled from every authed page."""
    _require(request)
    running, done = importer.running()
    return JSONResponse({"running": running, "done": done})


@app.get("/imports/{import_id}/progress")
def import_progress(request: Request, import_id: int) -> JSONResponse:
    """Live import progress for the import page's no-reload monitor."""
    _require(request)
    prog = _cached(f"prog:{import_id}", lambda: db.import_progress(import_id))
    if prog is None:
        return _json_err("import not found", 404)
    with importer._lock:
        live = importer._jobs.get(import_id)
        prog["running"] = bool(live)
        if live:
            prog["live"] = {
                "added": live["added"],
                "skipped": live.get("skipped", 0),
                "failed": live["failed"],
                "total": live["total"],
                "elapsed": round(time.time() - live["started"], 1),
            }
    return JSONResponse(prog)


@app.get("/lists/{list_id}/items/{item_id}/detail")
def item_drawer(request: Request, list_id: int, item_id: int) -> JSONResponse:
    """Everything the item drawer shows: ids, resolvability, containing lists."""
    _require(request)
    det = _cached(f"det:{list_id}:{item_id}", lambda: db.item_detail(item_id, list_id))
    if det is None:
        return _json_err("item not found", 404)
    app_name = "radarr" if det["mediatype"] != "show" else "sonarr"
    idf = "tmdb_id" if app_name == "radarr" else "tvdb_id"
    key = str(det[idf] or "")
    lib = arr.library(app_name)
    det["in_library"] = key in lib
    det["monitored"] = lib.get(key, {}).get("monitored") if key in lib else False
    return JSONResponse(det)


@app.post("/items/{item_id}/star")
def item_star(request: Request, item_id: int, starred: str = Form(...)):
    _require(request)
    db.set_star(item_id, starred == "true")
    bust_cache()
    return JSONResponse({"ok": True, "starred": starred == "true"})


@app.post("/items/{item_id}/note")
def item_note(request: Request, item_id: int, note: str = Form("")):
    _require(request)
    if len(note) > 2000:
        return _json_err("note too long", 400)
    db.set_note(item_id, note)
    bust_cache()
    return JSONResponse({"ok": True})


@app.post("/imports/{import_id}/reissue")
def import_reissue(request: Request, import_id: int) -> RedirectResponse:
    """Arr re-issue affordance (spec §5.4): replay the failed rows of one
    past import as a new job, with the stored options verbatim."""
    _require(request)
    try:
        res = importer.start_reissue(import_id)
    except RuntimeError:
        return RedirectResponse(f"/imports/{import_id}", status_code=302)
    bust_cache()
    target = (
        f"/imports/{res['import_id']}" if res["import_id"] else f"/imports/{import_id}"
    )
    return RedirectResponse(target, status_code=302)
