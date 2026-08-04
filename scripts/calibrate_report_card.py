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
constant in ``report_card.py``; paste the output into the CHANGELOG entry
and the PR body.

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

# Below this many rated runs a histogram is noise and the gate abstains rather
# than failing on thin data — the same reasoning as MIN_REFERENCE_ACTIVITIES.
MIN_SAMPLE = 10
# Share of rated runs at or under FAILING_MAX_STARS above which the yardstick,
# not the athlete, is the likelier explanation. Deliberately loose: the 0.40.0
# axis sat at 84% and the corrected one at 40%, so 0.60 separates them with room
# on both sides. Unchanged across the 0.50.0 star cutover — the threshold and
# its semantics are the same, only the definition of "failing" is restated.
DEFAULT_MAX_FAIL_SHARE = 0.60
# A score at or under this is a failing outcome for the punitive-skew test:
# 2.0 stars is exactly the old D/F territory (GRADE_POINTS <= 1 on the 0-4
# scale). Same boundary `ledger.BAD_CARD_MAX_STARS` uses, so the two definitions
# of "this session went badly" cannot drift.
FAILING_MAX_STARS = 2.0
# --- the collapsed-scale signature (0.50.0) --------------------------------
# "Dead bands" had no honest continuous analogue. With 17 quarter-star buckets
# and n=41, continuity would trivially "fail" on nine unused buckets while being
# perfectly healthy — the letter version's two-empty-bands rule counted five
# coarse buckets, not seventeen fine ones.
#
# What actually broke in 0.40.0 was BIMODALITY: the HR-cap time-fraction axis
# emitted 32 F, 7 A and 4 D over 43 runs, with B and C empty — mass piled at
# both extremes and nothing in between. So gate that directly, as a CONJUNCTION.
#
# The conjunction is what preserves the gate's deliberate asymmetry.
# Concentration in a GOOD score is not evidence of anything and is reported,
# never gated: HR's rolling band sits at 92% max with 8% interior and passes,
# because its floor share is 0%. A metric fails only when it is punishing
# heavily AND refusing to rate anything in between. An interior-only rule was
# tried and would have false-failed that healthy line.
DEFAULT_MIN_FLOOR_SHARE = 0.25
DEFAULT_MIN_INTERIOR_SHARE = 0.25

# Which constants govern which metric, so a failure names the thing to look at
# instead of leaving the reader to grep. Keyed by the report line, not by the
# metric dict key, because `hr` splits into two independently-tuned regimes.
_CURVE = ("STAR_KNOTS", "STAR_SCALE", "STAR_NOISE")
GOVERNING_CONSTANTS = {
    "distance": (*_CURVE, "DISTANCE_FACTORS", "PLAN_TIGHTEN"),
    "pace": (*_CURVE, "PACE_FACTORS", "PLAN_TIGHTEN", "STEADY_WIDEN"),
    "hr (rolling band)": (*_CURVE, "HR_BANDS"),
    "hr (prescribed cap)": (*_CURVE, "HR_CAP_NOISE_BPM", "HR_CAP_BPM_SCALE"),
    "continuity": (*_CURVE, "CONTINUITY_TOLERANCE", "MIN_CONTINUITY_SPLITS"),
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


def collect(cards: list[dict]) -> dict[str, list[float]]:
    """Per-report-line star scores across the rated cards.

    A list, not a Counter: the gate now measures the SHAPE of a continuous
    distribution (floor share, interior share, spread), and bucketing before
    that measurement would throw away the thing being measured. Buckets are
    computed for display only, in `format_report`.
    """
    from local_fitness.agent import report_card as rc

    tally: dict[str, list[float]] = {k: [] for k in GOVERNING_CONSTANTS}
    for card in cards:
        for name, metric in (card.get("metrics") or {}).items():
            if name not in rc.COMPLIANCE_METRICS:
                continue
            score = metric.get("stars")
            if score is None:            # n/a — abstained, not rated
                continue
            line = _hr_line(metric) if name == "hr" else name
            tally[line].append(score)
    return tally


def verdict(scores: list[float], *, max_fail_share: float,
            min_floor_share: float, min_interior_share: float) -> tuple[str, str]:
    """(status, reason) for one metric's star distribution.

    ``skip`` under ``MIN_SAMPLE``; ``FAIL`` on punitive skew or a collapsed
    scale; ``ok`` otherwise. Concentration in a HIGH score is never a failure —
    see the module docstring for why that asymmetry is the whole point.

    All three shares are computed on the DISPLAY-quantized value, so the gate
    judges the scale a reader actually sees rather than a float they never do.
    """
    from local_fitness.agent import report_card as rc

    n = len(scores)
    if n < MIN_SAMPLE:
        return "skip", f"only {n} rated run(s); need {MIN_SAMPLE}"
    shown = [rc.star_bucket(s) for s in scores]
    failing = sum(1 for s in shown if s <= FAILING_MAX_STARS)
    fail_share = failing / n
    floor_share = sum(1 for s in shown if s <= rc.STAR_FLOOR) / n
    interior = sum(1 for s in shown if rc.STAR_FLOOR < s < rc.STAR_MAX)
    interior_share = interior / n
    buckets = len({s for s in shown})
    top_share = max(Counter(shown).values()) / n
    if fail_share > max_fail_share:
        return "FAIL", (f"{fail_share:.0%} of runs rated "
                        f"{FAILING_MAX_STARS:.1f} stars or less "
                        f"(max {max_fail_share:.0%}) — punitive skew")
    if floor_share >= min_floor_share and interior_share < min_interior_share:
        return "FAIL", (f"collapsed scale — {floor_share:.0%} on the floor with "
                        f"only {interior_share:.0%} in between")
    return "ok", (f"{buckets} buckets used, top {top_share:.0%}, "
                  f"interior {interior_share:.0%}, "
                  f"<={FAILING_MAX_STARS:.1f}* {fail_share:.0%}")


def _strip(scores: list[float]) -> str:
    """A 17-cell occupancy strip over the quarter-star buckets, 1.00 -> 5.00.

    Replaces the A/B/C/D/F histogram. Printing 17 counts would be unreadable at
    a glance and the counts are not the question — occupancy is, since the
    failure this gate exists to catch is a scale that only ever emits its
    extremes. `#` marks an occupied bucket, `.` an empty one.
    """
    from local_fitness.agent import report_card as rc

    shown = {rc.star_bucket(s) for s in scores}
    steps = [rc.STAR_FLOOR + i * rc.STAR_DISPLAY_STEP
             for i in range(int((rc.STAR_MAX - rc.STAR_FLOOR)
                                / rc.STAR_DISPLAY_STEP) + 1)]
    return "".join("#" if any(abs(b - s) < 1e-9 for b in shown) else "."
                   for s in steps)


def format_report(tally: dict[str, list[float]], cards: list[dict], *,
                  max_fail_share: float, min_floor_share: float,
                  min_interior_share: float, days: int) -> tuple[str, bool]:
    """Render the whole report; return (text, any_failure)."""
    from local_fitness.agent import report_card as rc

    out = [f"Report-card calibration — {len(cards)} running efforts, trailing {days} days",
           "",
           "occupancy strip: 17 quarter-star buckets, 1.00 (left) -> 5.00 (right)",
           ""]
    header = f"{'metric':<22} {'1.00 .. 5.00':<17}  {'mean':>5} {'n':>4}  verdict"
    out += [header, "-" * len(header)]

    failed = False
    for line in GOVERNING_CONSTANTS:
        scores = tally[line]
        n = len(scores)
        status, reason = verdict(
            scores, max_fail_share=max_fail_share,
            min_floor_share=min_floor_share,
            min_interior_share=min_interior_share)
        failed = failed or status == "FAIL"
        mean = f"{sum(scores) / n:.2f}" if n else "—"
        out.append(f"{line:<22} {_strip(scores):<17}  {mean:>5} {n:>4}  "
                   f"{status} — {reason}")
        if status == "FAIL":
            out.append(f"{'':<22} governed by: {', '.join(GOVERNING_CONSTANTS[line])}")

    # Informational: the card-level view. Not gated — `overall` is derived from
    # the rows above, so failing it too would report one defect twice. A high
    # cap rate is the leading indicator that preceded BOTH the 0.40.0 load bug
    # and the 0.40.2 HR-cap bug, so it is worth printing even though it is not
    # the gate.
    #
    # The collapsed-scale signature must stay OFF the overall permanently: a
    # weighted mean of four metrics cannot reach the floor unless all four do,
    # so the bottom buckets are structurally unreachable (measured minimum over
    # 240 real cards: 1.72) and gating them would fail on healthy data forever.
    scores = [c["overall"]["stars"] for c in cards
              if (c.get("overall") or {}).get("stars") is not None]
    capped = sum(1 for c in cards if (c.get("overall") or {}).get("capped"))
    out += ["", "overall (informational, not gated)"]
    if scores:
        out.append(f"  {_strip(scores)}  mean {sum(scores) / len(scores):.2f}, "
                   f"median {sorted(scores)[len(scores) // 2]:.2f}")
        at_max = sum(1 for s in scores if rc.star_bucket(s) >= rc.STAR_MAX)
        out.append(f"  {at_max}/{len(scores)} at 5.00 "
                   f"({at_max / len(scores):.0%})")
    if cards:
        out.append(f"  cap fired on {capped}/{len(cards)} cards "
                   f"({capped / len(cards):.0%})")
    return "\n".join(out), failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=90,
                    help="trailing window to grade (default: 90)")
    ap.add_argument("--db", type=Path, default=None,
                    help="database path (default: the app's resolved DB)")
    ap.add_argument("--max-fail-share", type=float, default=DEFAULT_MAX_FAIL_SHARE,
                    help="fail above this share of runs rated <=2.0 stars "
                         "(default: 0.60)")
    ap.add_argument("--min-floor-share", type=float, default=DEFAULT_MIN_FLOOR_SHARE,
                    help="collapsed-scale trigger: floor share at or above "
                         "this, AND interior below --min-interior-share "
                         "(default: 0.25)")
    ap.add_argument("--min-interior-share", type=float,
                    default=DEFAULT_MIN_INTERIOR_SHARE,
                    help="collapsed-scale trigger: see --min-floor-share "
                         "(default: 0.25)")
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
                f"{k}={'n/a' if v.get('stars') is None else format(v['stars'], '.2f')}"
                for k, v in c["metrics"].items())
            overall = c["overall"].get("stars")
            shown = "n/a" if overall is None else f"{overall:.2f}"
            print(f"{c['activity']['date']}  overall={shown}  {metrics}")
        print()

    text, failed = format_report(
        collect(cards), cards,
        max_fail_share=args.max_fail_share,
        min_floor_share=args.min_floor_share,
        min_interior_share=args.min_interior_share,
        days=args.days)
    print(text)
    if failed:
        print("\nFAIL — at least one metric has stopped discriminating. "
              "Recalibrate the named constants against this distribution "
              "before shipping.")
        return 1
    print("\nOK — every rated metric still uses its scale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
