#!/usr/bin/env python3
"""Check every report-card grading constant against the live distribution.

CLAUDE.md's rule for this module is "calibrate bands against real data, not
intuition" — and until now nothing executed it. Every grading defect the report
card has shipped was found by a human reading a rendered card, never by the
suite, because ``tests/test_report_card.py`` asserts that the rubric computes
what it says it computes and nothing asserts that what it says is *right*.

The failure signature is always the same and it is measurable: a band table
stops discriminating. The 0.40.0 HR-cap time axis emitted 32 F / 7 A / 4 D over
90 days with **B and C empty** — a "rubric" with two outcomes. The 0.88 easy-HR
ceiling before it demanded a number that appeared in 1 of 13 runs. Both were
visible in one query against data that was sitting on disk the whole time.

So this recomputes every graded metric across a trailing window using the real
production path (``load_report_card_inputs`` -> ``build_card``, never a
reimplementation that could drift from it), prints a per-metric letter
histogram, and FAILS on either degeneracy signature:

  * **punitive skew** — more than ``--max-fail-share`` of graded runs land in
    D or F. A rubric measures compliance with a prescription the athlete is
    *trying* to follow, so heavy compliance is the expected state and heavy
    failure is not: either the athlete is missing constantly (which the coach
    is already shouting about) or the yardstick is wrong. The 0.40.0 axis sat
    at 84% D/F.
  * **dead bands** — ``--max-empty`` or more letters never used at all. The
    table cannot reach those grades on this data, so they are decoration. The
    0.40.0 axis never emitted a B or a C.

The asymmetry is deliberate and is the correction to this check's first draft,
which failed any letter above a flat 60% share and therefore flagged *healthy*
metrics: distance grades 79% A because the distances are being hit, and that
is the system working. Concentration in a passing grade is not evidence of
anything, so it is reported and not gated.

A dead band is a weaker claim than a wrong grade — it says the metric is
UNPROVEN over this window, not broken. On a manual gate that is the right
trade: the cost of a second look is a glance, and it is the signature that
would have caught both the 0.40.0 HR axis and the 0.26.0 "HR and load had
quietly become constants" failure.

NOT wired into CI, and cannot be: it needs a populated ``data/fitness.db`` that
CI does not have, and fabricating one would just be asking the fixture whether
the fixture agrees with itself. It is a MANUAL gate — run it before changing any
constant in ``report_card.py`` and paste the output into the devlog entry.

Strictly READ-ONLY: the database is opened ``mode=ro`` through a URI, so a write
is refused by SQLite rather than merely avoided by convention.

Usage:
    uv run python scripts/calibrate_report_card.py
    uv run python scripts/calibrate_report_card.py --days 180 --verbose
    uv run python scripts/calibrate_report_card.py --require-db   # missing DB fails

Exit codes: 0 clean (or skipped, no DB), 1 a distribution is degenerate,
2 the run could not be completed.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Every letter the rubric can emit, in order. A band table that never reaches
# one of these over a whole season is not grading, it is thresholding.
LETTERS = ("A", "B", "C", "D", "F")
# Below this many graded runs a histogram is noise and the gate abstains rather
# than failing on thin data — the same reasoning as MIN_REFERENCE_ACTIVITIES.
MIN_SAMPLE = 10
# Share of graded runs in D or F above which the yardstick, not the athlete, is
# the likelier explanation. Deliberately loose: the 0.40.0 axis sat at 84% and
# the corrected one at 40%, so 0.60 separates them with room on both sides.
DEFAULT_MAX_FAIL_SHARE = 0.60
# Unused letters at or above which the table is treated as not describing this
# quantity. Two, because one empty band is ordinary (a compliant athlete
# genuinely never earns an F) while two means the scale has collapsed.
DEFAULT_MAX_EMPTY = 2
# Letters that count as a failing outcome for the punitive-skew test.
FAILING = ("D", "F")

# Which constants govern which metric, so a failure names the thing to look at
# instead of leaving the reader to grep. Keyed by the report line, not by the
# metric dict key, because `hr` splits into two independently-tuned regimes.
GOVERNING_CONSTANTS = {
    "distance": ("GRADE_BANDS", "DISTANCE_FACTORS", "PLAN_TIGHTEN"),
    "pace": ("GRADE_BANDS", "PACE_FACTORS", "PLAN_TIGHTEN", "STEADY_WIDEN"),
    "hr (rolling band)": ("GRADE_BANDS", "HR_BANDS"),
    "hr (prescribed cap)": ("GRADE_BANDS", "HR_CAP_NOISE_BPM", "HR_CAP_BPM_SCALE"),
    "continuity": ("GRADE_BANDS", "CONTINUITY_TOLERANCE", "MIN_CONTINUITY_SPLITS"),
}


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection SQLite itself will refuse to write through.

    ``mode=ro`` is the point: the guarantee is enforced by the engine, not by
    this script remembering to only SELECT. Nate's live data is not ours.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def running_activity_ids(conn: sqlite3.Connection, days: int, today: date) -> list[int]:
    """Ids of on-foot running efforts in the trailing window, oldest first.

    On-foot label FIRST, then measured pace — the project-wide rule (pace alone
    would promote a fast bike ride to a run; the label alone lets walking-desk
    sessions through, because they log as ``treadmill_running``). A paceless row
    has an unknown mode and is excluded rather than guessed at, exactly as
    ``rolling_reference`` excludes it from both pools.
    """
    from local_fitness import plans
    from local_fitness.agent import interpret

    start = (today - timedelta(days=days - 1)).isoformat()
    # Bounded at BOTH ends. Without the upper bound `--days 5` means "everything
    # since five days before the anchor", which silently grades the whole
    # history whenever the anchor is in the past.
    rows = conn.execute(
        "SELECT activity_id, activity_type, avg_pace_sec_per_km FROM activities "
        "WHERE date >= ? AND date <= ? AND distance_meters > 0 "
        "ORDER BY date, start_time",
        (start, today.isoformat()),
    ).fetchall()
    return [
        r["activity_id"] for r in rows
        if plans._is_running(r["activity_type"])
        and interpret.is_running_effort(r["avg_pace_sec_per_km"]) is True
    ]


def grade_window(conn: sqlite3.Connection, activity_ids: list[int]) -> list[dict]:
    """Build a real card per activity and return the graded rows.

    Uses the production entry points so the histogram describes the rubric that
    actually ships. A reimplementation here would be free to disagree with
    ``build_card`` and we would never know which one was wrong.
    """
    from local_fitness.agent import report_card as rc

    cards = []
    for aid in activity_ids:
        inputs = rc.load_report_card_inputs(conn, activity_id=aid)
        if inputs is None:
            continue
        card = rc.build_card(
            inputs["activity"], inputs["splits"], inputs.get("plan_workout"),
            inputs["reference"], inputs.get("context"),
            hr_zones=inputs.get("hr_zones"),
        )
        cards.append(card)
    return cards


def _hr_line(metric: dict) -> str:
    """Which HR regime graded this row. The two use different constants and a
    pooled histogram would hide a degenerate one behind a healthy one."""
    return "hr (prescribed cap)" if metric.get("cap") else "hr (rolling band)"


def collect(cards: list[dict]) -> dict[str, Counter]:
    """Per-report-line base-letter counts across the graded cards."""
    from local_fitness.agent import report_card as rc

    tally: dict[str, Counter] = {k: Counter() for k in GOVERNING_CONSTANTS}
    for card in cards:
        for name, metric in (card.get("metrics") or {}).items():
            if name not in rc.COMPLIANCE_METRICS:
                continue
            letter = rc.base_letter(metric.get("grade"))
            if letter not in LETTERS:      # n/a — abstained, not graded
                continue
            line = _hr_line(metric) if name == "hr" else name
            tally[line][letter] += 1
    return tally


def verdict(counts: Counter, *, max_fail_share: float, max_empty: int) -> tuple[str, str]:
    """(status, reason) for one metric's letter distribution.

    ``skip`` under ``MIN_SAMPLE``; ``FAIL`` on punitive skew or dead bands;
    ``ok`` otherwise. Concentration in a PASSING letter is never a failure —
    see the module docstring for why that asymmetry is the whole point.
    """
    n = sum(counts.values())
    if n < MIN_SAMPLE:
        return "skip", f"only {n} graded run(s); need {MIN_SAMPLE}"
    failing = sum(counts[x] for x in FAILING)
    fail_share = failing / n
    empty = [x for x in LETTERS if not counts[x]]
    used = len(LETTERS) - len(empty)
    if fail_share > max_fail_share:
        return "FAIL", (f"{fail_share:.0%} of runs graded D/F "
                        f"(max {max_fail_share:.0%}) — punitive skew")
    if len(empty) >= max_empty:
        return "FAIL", f"dead bands — {', '.join(empty)} never used ({used}/5)"
    top_letter, top = counts.most_common(1)[0]
    return "ok", f"{used}/5 bands used, {top_letter} {top / n:.0%}, D/F {fail_share:.0%}"


def format_report(tally: dict[str, Counter], cards: list[dict], *,
                  max_fail_share: float, max_empty: int, days: int) -> tuple[str, bool]:
    """Render the whole report; return (text, any_failure)."""
    from local_fitness.agent import report_card as rc

    out = [f"Report-card calibration — {len(cards)} running efforts, trailing {days} days",
           ""]
    header = f"{'metric':<22} {'A':>4}{'B':>4}{'C':>4}{'D':>4}{'F':>4}   {'n':>4}  verdict"
    out += [header, "-" * len(header)]

    failed = False
    for line in GOVERNING_CONSTANTS:
        counts = tally[line]
        n = sum(counts.values())
        status, reason = verdict(
            counts, max_fail_share=max_fail_share, max_empty=max_empty)
        failed = failed or status == "FAIL"
        cells = "".join(f"{counts[x]:>4}" for x in LETTERS)
        out.append(f"{line:<22} {cells}   {n:>4}  {status} — {reason}")
        if status == "FAIL":
            out.append(f"{'':<22} governed by: {', '.join(GOVERNING_CONSTANTS[line])}")

    # Informational: the card-level view. Not gated — `overall` is derived from
    # the rows above, so failing it too would report one defect twice. A high
    # F-cap rate is the leading indicator that preceded BOTH the 0.40.0 load bug
    # and the 0.40.2 HR-cap bug, so it is worth printing even though it is not
    # the gate.
    overall = Counter(rc.base_letter(c["overall"].get("grade")) for c in cards)
    capped = sum(1 for c in cards if c["overall"].get("capped_by"))
    out += ["", "overall (informational, not gated)"]
    out.append("  letters: " + "  ".join(
        f"{x}={overall.get(x, 0)}" for x in LETTERS))
    if cards:
        out.append(f"  F-cap fired on {capped}/{len(cards)} cards "
                   f"({capped / len(cards):.0%})")
    return "\n".join(out), failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=90,
                    help="trailing window to grade (default: 90)")
    ap.add_argument("--db", type=Path, default=None,
                    help="database path (default: the app's resolved DB)")
    ap.add_argument("--max-fail-share", type=float, default=DEFAULT_MAX_FAIL_SHARE,
                    help="fail above this share of D/F grades (default: 0.60)")
    ap.add_argument("--max-empty", type=int, default=DEFAULT_MAX_EMPTY,
                    help="fail at or above this many unused letters (default: 2)")
    ap.add_argument("--require-db", action="store_true",
                    help="treat a missing/empty database as a failure, not a skip")
    ap.add_argument("--verbose", action="store_true",
                    help="also list every graded run")
    args = ap.parse_args(argv)

    from local_fitness import db

    path = args.db or db.DEFAULT_DB_PATH
    if not Path(path).exists():
        print(f"SKIPPED — no database at {path}.\n"
              "This gate needs real history; a fresh clone has nothing to "
              "calibrate against. Re-run after `uv run fitness pull`.")
        return 2 if args.require_db else 0

    # sqlite3.connect() is lazy — a file that is not a database opens fine and
    # only raises on first read, so the guard has to cover the queries too.
    try:
        conn = open_readonly(Path(path))
        try:
            ids = running_activity_ids(conn, args.days, date.today())
            cards = grade_window(conn, ids)
        finally:
            conn.close()
    except sqlite3.Error as exc:            # unreadable / not a database
        print(f"ERROR — could not read {path}: {exc}")
        return 2

    if not cards:
        print(f"SKIPPED — no running efforts in the trailing {args.days} days.")
        return 2 if args.require_db else 0

    if args.verbose:
        for c in cards:
            metrics = "  ".join(
                f"{k}={v.get('grade') or 'n/a'}" for k, v in c["metrics"].items())
            print(f"{c['activity']['date']}  overall={c['overall'].get('grade')}  {metrics}")
        print()

    text, failed = format_report(
        collect(cards), cards,
        max_fail_share=args.max_fail_share, max_empty=args.max_empty,
        days=args.days)
    print(text)
    if failed:
        print("\nFAIL — at least one band table has stopped discriminating. "
              "Recalibrate the named constants against this distribution "
              "before shipping.")
        return 1
    print("\nOK — every graded metric still uses its bands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
