"""Sync engine (docs/specs/ListHoard.spec.md §9).

One scheduled daily pass plus manual per-source/per-list triggers out of the
UI. Idempotent: dedup happens through `db.resolve_item`, so lists overlapping
in items share rows. Provider-level failures keep previous contents and land
in `lists.last_error`; sources failing repeatedly are quarantined by the
adapter layer and surfaced on the health board (§9.5).
"""

import logging
import threading

from . import adapters, budget, db

log = logging.getLogger("listhoard.sync")

_stop = threading.Event()


def sync_list(source_slug: str, list_row: dict) -> int:
    """Fetch one list; returns the number of items synchronised."""
    count = 0
    adapter_ok = False
    try:
        fetched = list(adapters.fetch_list(source_slug, list_row["external_id"]))
        item_ids = db.resolve_items_batch(fetched)
        count = len(fetched)
        db.link_items(list_row["id"], item_ids)
        db.update_list_sync(list_row["id"], count)
        adapters.record_result(source_slug, True)
        adapter_ok = True
    except budget.BudgetExhausted:
        # A budget is a quota, not a failure (Shippability §8): leave the
        # quarantine counter untouched, mark the list honestly, and pause
        # the source by re-raising to sync_source.
        log.info("budget exhausted: %s paused at list %s", source_slug, list_row["id"])
        db.update_list_sync(list_row["id"], error="budget exhausted")
        raise
    except Exception as e:  # noqa: BLE001 — sync must survive
        log.warning("sync %s/%s failed: %s", source_slug, list_row["external_id"], e)
        db.update_list_sync(list_row["id"], error=str(e)[:400])
        adapters.record_result(source_slug, False)
    if adapter_ok and list_row.get("prev_count") is not None:
        return count - int(list_row["prev_count"])
    return count


def sync_source(slug: str) -> int:
    """Refresh every list of one source; returns the aggregate delta."""
    if adapters.quarantined(slug):
        log.info("source %s quarantined; skipping", slug)
        return 0
    lists = db.load_lists(slug)
    delta = 0
    for lst in lists:
        if _stop.is_set():
            break
        prev = lst["item_count"] if lst["last_synced_at"] is not None else None
        row = dict(lst)
        row["prev_count"] = prev
        try:
            delta += sync_list(slug, row)
        except budget.BudgetExhausted:
            # Pause-not-fail (Shippability §8): keep the quarantine counter
            # untouched, report the delta earned so far, drop out of the pass.
            log.info("source %s paused on its budget; delta so far %d", slug, delta)
            db.health_update(slug, None, delta)
            return delta
        adapters.sleep_pacing()
    db.health_update(
        slug, None if not adapters.quarantined(slug) else "quarantined", delta
    )
    return delta


def sync_all() -> None:
    """One full pass over every catalogued source. Sources whose adapter
    fails repeatedly are skipped via the quarantine counter (§9.5). Also the
    session garbage collector (Shippability §7)."""
    purged = db.purge_sessions()
    if purged:
        log.info("session GC: %d expired rows removed", purged)
    for slug in sorted(db.catalog_totals()["per_source"]):
        if _stop.is_set():
            return
        if slug not in adapters.ADAPTERS:
            log.warning("sync: source %s has no adapter; skipping", slug)
            continue
        sync_source(slug)


def start_daily(
    offset_hours: float = 0.0, period_hours: float = 24.0
) -> threading.Thread:
    def run() -> None:
        while not _stop.is_set():
            try:
                sync_all()
            except Exception:
                log.exception("daily sync pass failed")
            _stop.wait(period_hours * 3600)

    t = threading.Thread(target=run, name="listhoard-sync", daemon=True)
    t.start()
    return t


def trigger_source(slug: str) -> threading.Thread:
    def run() -> None:
        sync_source(slug)

    t = threading.Thread(target=run, name=f"sync-{slug}", daemon=True)
    t.start()
    return t


def shutdown() -> None:
    _stop.set()
