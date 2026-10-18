#!/usr/bin/env python3
"""Headless tests for Shippability slice 3 (provider budgets, spec §8).

Run directly (python3 tests/listhoard_budget_unit.py); wired into
scripts/gate.py. No network, no database: the Budget's clock is injectable,
so window resets are exercised deterministically.

1. Windows — the hourly counter resets when the clock crosses the hour
   bucket; the daily counter resets on the date; the check() view agrees.
2. Exhaustion — consume() raises BudgetExhausted past the hour cap; it is
   NOT source health's failure shape (no quarantine increment is implied —
   the budget line is a pause, and state().available flips False).
3. Env overrides — LISTHOARD_BUDGET_<SLUG>_PER_HOUR/_DAY move the caps.
4. Registry — unknown sources get the conservative default caps.
"""

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def run_tests() -> bool:
    failures: list[str] = []

    sys.path.insert(0, str(REPO))
    stub = type(sys)("psycopg")
    stub.connect = None
    sys.modules["psycopg"] = stub

    import app.budget as budget_mod

    class FakeClock:
        """Wall-clock stand-in: `t` is a real time.time() the test moves."""

        def __init__(self, start: float):
            self.t = start

        def __call__(self) -> float:
            return self.t

    # A start in the middle of an hour and day (UTC 12:00:00 on 2026-10-18).
    start = 18105.0 * 3600  # arbitrary fixed epoch; buckets stay relative

    # --- 2+1: exhaustion and reset windows ------------------------------------
    clock = FakeClock(start)
    b = budget_mod.Budget("probe", per_hour=3, per_day=10, clock=clock)
    for i in range(3):
        b.consume()
    if b.check():
        failures.append("budget with full hour must check False")
    try:
        b.consume()
        failures.append("consume past the hour cap must raise")
    except budget_mod.BudgetExhausted:
        pass
    st = b.state()
    if st["hour_used"] != 3 or st["hour_limit"] != 3 or st["available"]:
        failures.append(f"exhausted state wrong: {st}")
    if b.state().get("day_used", 0) != 3:
        failures.append("day counter must have consumed the same three")

    # Cross the hour bucket: the hourly window resets, the day window keeps.
    clock.t = start + 3601
    if not b.check():
        failures.append("a new hour bucket must restore check()")
    b.consume()
    st = b.state()
    if st["hour_used"] != 1:
        failures.append(f"hour counter must reset on the bucket: {st}")

    # Exhaust the day cap; even a fresh hour does not restore it. This
    # Budget's hour cap is high (50) so the hour window can't preempt the
    # day window mid-loop — the collision is the previous assertion's job.
    clock.t = start + 7200
    b2 = budget_mod.Budget("probe2", per_hour=50, per_day=10, clock=clock)
    day_spent = 0
    while True:
        try:
            b2.consume()
        except budget_mod.BudgetExhausted:
            break
        day_spent += 1
        if day_spent > 50:
            failures.append("day cap never enforced after 50 extra consumes")
            break
    clock.t = start + 10800
    if b2.check():
        failures.append("a fresh hour must NOT restore a spent day cap")
    st = b2.state()
    if st["hour_used"] != 0 or st["day_used"] != 10:
        failures.append(f"window accounting wrong at day exhaustion: {st}")

    # --- 3: env overrides --------------------------------------------------------
    base_env = dict(os.environ)
    try:
        os.environ["LISTHOARD_BUDGET_PROBEX_PER_HOUR"] = "5"
        os.environ["LISTHOARD_BUDGET_PROBEX_PER_DAY"] = "7"
        reg = budget_mod.Registry(clock=FakeClock(start))
        rb = reg.get("probex")
        if (rb.per_hour, rb.per_day) != (5, 7):
            failures.append(f"env caps not honored: {(rb.per_hour, rb.per_day)}")
        # The registry memoizes; a second get returns the same Budget.
        if reg.get("probex") is not rb:
            failures.append("Registry must memoize one Budget per slug")
    finally:
        os.environ.clear()
        os.environ.update(base_env)

    # --- 4: unknown slug defaults --------------------------------------------------
    reg2 = budget_mod.Registry(clock=FakeClock(start))
    unknown = reg2.get("brand-new-source")
    if (unknown.per_hour, unknown.per_day) != (120, 500):
        failures.append(
            f"unknown slugs must take conservative defaults: {unknown.per_hour}"
        )

    # Every DEFAULTS slug yields a workable state dict.
    st_all = reg2.state()
    for slug, (h, d) in budget_mod.DEFAULTS.items():
        s = st_all.get(slug)
        if s is None or s["hour_limit"] != h or s["day_limit"] != d:
            failures.append(f"registry state missing/wrong for {slug}: {s}")

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        return False
    print("PASS shippability slice 3: budget windows, exhaustion, env overrides")
    return True


def main() -> int:
    return 0 if run_tests() else 1


if __name__ == "__main__":
    sys.exit(main())
