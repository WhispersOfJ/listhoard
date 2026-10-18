"""Provider request budgets (the shippability specification §8).

A Budget is a hard per-provider request cap enforced in code — a quota, NOT
a failure: an exhausted budget *pauses* that source until its window resets
(the quarantine counter is never touched), and the overrun logs once per day
at WARNING, never per request nor a traceback.

Windows are fixed wall-clock buckets (one hour / one calendar day, UTC) so
the reset time is predictable from outside: `now // 3600` for hours, the
date for days. Counters are in-process; a restart resets them, which errs on
the side of politeness the app already guarantees.

Defaults per provider (requests/hour, requests/day) sit below each API's
documented quota with headroom for the home stack's shared keys; env
overrides follow `LISTHOARD_BUDGET_<SLUG>_PER_HOUR` / `_PER_DAY`.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("listhoard.budget")

# (slug → (per_hour, per_day)). Scraped providers get the tightest caps:
# they are the sources most likely to be someone else's bandwidth.
DEFAULTS: dict[str, tuple[int, int]] = {
    "tmdb": (900, 4000),
    "trakt": (500, 2000),
    "mdblist": (200, 1000),
    "letterboxd": (120, 500),
    "imdb": (60, 250),
    "rottentomatoes": (60, 250),
    "tvdb": (200, 1000),
}


class BudgetExhausted(Exception):
    """One provider's budget window is spent; the caller pauses, not fails."""


class Budget:
    """One provider's rolling counters. Lock-guarded: the sync thread and
    request threads both consume."""

    def __init__(self, slug: str, per_hour: int, per_day: int, clock=None):
        self.slug = slug
        self.per_hour = per_hour
        self.per_day = per_day
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._hour_bucket = -1
        self._day_bucket = ""
        self._hour_count = 0
        self._day_count = 0
        self._warned_on: str | None = None

    # -- windows ---------------------------------------------------------------
    def _buckets(self) -> tuple[int, str]:
        now = self._clock()
        hour = int(now // 3600)
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        return hour, day

    # -- consumption -------------------------------------------------------------
    def consume(self) -> None:
        """Charge one request; raise BudgetExhausted when a window is spent."""
        with self._lock:
            hour, day = self._buckets()
            if hour != self._hour_bucket:
                self._hour_bucket, self._hour_count = hour, 0
            if day != self._day_bucket:
                self._day_bucket, self._day_count = day, 0
                self._warned_on = None
            if self._hour_count >= self.per_hour or self._day_count >= self.per_day:
                today = day
                if self._warned_on != today:
                    self._warned_on = today
                    log.warning(
                        "budget exhausted for %s (hour %d/%d, day %d/%d) — "
                        "source paused until window reset",
                        self.slug,
                        self._hour_count,
                        self.per_hour,
                        self._day_count,
                        self.per_day,
                    )
                raise BudgetExhausted(self.slug)
            self._hour_count += 1
            self._day_count += 1

    def check(self) -> bool:
        """True when at least one more request fits the windows."""
        with self._lock:
            return self._check_unlocked()

    def _check_unlocked(self) -> bool:
        """check()'s body without the lock — state() already holds it (a
        threading.Lock is not reentrant; calling check() from state()
        deadlocked the health board before the fix).

        A bucket boundary refreshes THAT window only: a fresh hour must not
        restore a spent day cap, and vice versa — each window's remaining
        allowance is computed independently."""
        hour, day = self._buckets()
        hour_left = (
            self.per_hour
            if hour != self._hour_bucket
            else (self.per_hour - self._hour_count)
        )
        day_left = (
            self.per_day
            if day != self._day_bucket
            else (self.per_day - self._day_count)
        )
        return hour_left > 0 and day_left > 0

    # -- visibility (health board) ---------------------------------------------
    def state(self) -> dict:
        """The health-board view: used/limit for the current windows."""
        with self._lock:
            hour, day = self._buckets()
            if hour != self._hour_bucket:
                hour_used = 0
            else:
                hour_used = self._hour_count
            if day != self._day_bucket:
                day_used = 0
            else:
                day_used = self._day_count
            return {
                "hour_used": hour_used,
                "hour_limit": self.per_hour,
                "day_used": day_used,
                "day_limit": self.per_day,
                "available": self._check_unlocked(),
            }


class Registry:
    """All budgets, built once from DEFAULTS + env overrides."""

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._budgets: dict[str, Budget] = {}
        self._lock = threading.Lock()

    def get(self, slug: str) -> Budget:
        with self._lock:
            if slug not in self._budgets:
                per_hour, per_day = DEFAULTS.get(slug, (120, 500))
                per_hour = int(
                    os.environ.get(
                        f"LISTHOARD_BUDGET_{slug.upper()}_PER_HOUR", per_hour
                    )
                )
                per_day = int(
                    os.environ.get(f"LISTHOARD_BUDGET_{slug.upper()}_PER_DAY", per_day)
                )
                self._budgets[slug] = Budget(slug, per_hour, per_day, clock=self._clock)
            return self._budgets[slug]

    def state(self) -> dict:
        """Every budget's state, keyed by slug — the health board's view.
        All DEFAULTS are always present, touched or not, so a fresh instance
        shows its caps before the first request."""
        with self._lock:
            slugs = sorted(set(self._budgets) | set(DEFAULTS))
        return {slug: self.get(slug).state() for slug in slugs}


REGISTRY = Registry()
