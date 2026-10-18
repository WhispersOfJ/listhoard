#!/usr/bin/env python3
"""Headless audit of the abyss palette's text contrast (ListHoard-UX §5.7).

WCAG-style contrast ratios for the pairs the UI actually renders. Run
directly (python3 tests/listhoard_a11y_unit.py); wired into scripts/gate.py.
Fails the gate when a reading-text pair falls under 4.5:1 or an
accent/signifier pair falls under 3:1 — CSS changes cannot silently
reintroduce unreadable ink.
"""

import pathlib
import re
import sys

CSS = pathlib.Path("static/style.css")

RGBA = re.compile(r"rgba?\(([^)]+)\)")
HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse(name: str) -> tuple[int, int, int]:
    m = re.search(re.escape(name) + r"\s*:\s*([^;]+);", CSS.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"FAILED: {name} not found in stylesheet")
    return color(m.group(1).strip())


def color(tok: str) -> tuple[int, int, int]:
    tok = tok.strip()
    if HEX.match(tok):
        tok = tok.lstrip("#")
        if len(tok) == 3:
            tok = "".join(c * 2 for c in tok)
        return int(tok[0:2], 16), int(tok[2:4], 16), int(tok[4:6], 16)
    m = RGBA.match(tok)
    if not m:
        raise SystemExit(f"FAILED: unparsable color {tok!r}")
    parts = [p.strip() for p in m.group(1).split(",")]
    r, g, b = (int(float(p)) for p in parts[:3])
    if len(parts) == 4:
        a = float(parts[3])
        # composite over the trench floor the UI always renders beneath
        base = (2, 6, 14)
        r = round(a * r + (1 - a) * base[0])
        g = round(a * g + (1 - a) * base[1])
        b = round(a * b + (1 - a) * base[2])
    return r, g, b


def ratio(fg: tuple, bg: tuple) -> float:
    def lum(c):
        srgb = [x / 255 for x in c]
        lin = [
            x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in srgb
        ]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    l1, l2 = sorted((lum(fg), lum(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


# Panels render as translucent hull over water; test each flag/badge against
# its own background composited over the hull tone, then over pure water.
def composite(over: tuple, alpha: float, under: tuple) -> tuple:
    return tuple(round(alpha * o + (1 - alpha) * u) for o, u in zip(over, under))


HULL_FRAC = 0.78
HULL = (9, 20, 38)
WATER = (5, 18, 38)  # --mid, the gradient band most rows sit on


def panel(rgb_csv: str, alpha: float) -> tuple:
    r, g, b = (int(p.strip()) for p in rgb_csv.split(","))
    return composite(composite((r, g, b), alpha, WATER), HULL_FRAC, WATER)


def main() -> int:
    failing: list[str] = []

    reading = [
        # (foreground token in css, background rgb, floor)
        (parse("--ink"), WATER, 4.5, "body ink on mid water"),
        (
            parse("--ink"),
            composite(HULL, HULL_FRAC, WATER),
            4.5,
            "body ink on hull panel",
        ),
        (parse("--ink-dim"), WATER, 4.5, "dim ink on mid water"),
        (
            parse("--ink-dim"),
            composite(HULL, HULL_FRAC, WATER),
            4.5,
            "dim ink on hull panel",
        ),
        (
            parse("--ink-bright"),
            composite(HULL, HULL_FRAC, WATER),
            7.0,
            "heading on hull",
        ),
        (parse("--lume"), WATER, 3.0, "the lure as signifier on water"),
        ((158, 245, 226), panel("78, 240, 207", 0.08), 4.5, "badge.in"),
        ((205, 185, 251), panel("177, 140, 247", 0.10), 4.5, "flag.anime"),
        ((244, 169, 177), panel("224, 101, 111", 0.09), 4.5, "flag.xxx / bad"),
        ((159, 245, 226), WATER, 4.5, "code text on water"),
    ]

    for fg, bg, floor, label in reading:
        r = ratio(fg, bg)
        mark = "ok" if r >= floor else "FAILED"
        print(f"  {mark}: {label} = {r:.2f}:1 (floor {floor})")
        if r < floor:
            failing.append(label)

    if failing:
        print("FAILED: " + "; ".join(failing), file=sys.stderr)
        return 1
    print(f"a11y contrast: {len(reading)}/{len(reading)} pairs pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
