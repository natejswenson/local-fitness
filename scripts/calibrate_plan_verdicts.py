#!/usr/bin/env python3
"""Check the quality-day pace cut against the live verdict distribution.

The plan-side sibling of ``calibrate_report_card.py``, and it exists because
#242 shipped a grading constant with no way to ask the data whether it was
right. ``QUALITY_PACE_DONE_DEVIATION`` was derived from a ``report_card`` star
boundary — a defensible way to keep the two surfaces agreeing, and no evidence
at all about what *plan adherence* should mean. CLAUDE.md's rule for the card
("calibrate bands against real data, not intuition — and the check is now
executable") had no plan-side equivalent, so nothing could catch the answer
being wrong; the first round of review found ``done`` unreachable for every
quality day in the live history and the suite stayed green through it
(#242 r2, f-cb50f53f).

So this regrades every quality day on every plan through the production path —
``quality_pace_dates`` -> ``load_activities_by_date`` -> ``classify_workout``,
never a reimplementation that could drift from what ships — and gates two
degeneracy signatures.

**What counts as rated, and why it is not "every quality day".** More than half
the quality days in the live history have no qualifying run on them at all:
the athlete walked, or the day is still in the future. Those grade ``missed``
on volume before the pace cap is ever consulted, and counting them would let
absence masquerade as a verdict about the yardstick — the same reason
``calibrate_report_card`` grades only running efforts. A day is RATED here when
the pace cap actually had an opinion: a qualifying run carrying a rep-sized
split, so ``_fastest_rep_pace`` returned a number.

The two gates:

  * **punitive skew** — more than ``--max-missed-share`` of rated days land on
    ``missed``. Same reasoning, and the same default, as the card's: a plan is
    a prescription the athlete is *trying* to follow, so heavy compliance is
    the expected state and heavy failure means either the athlete is missing
    constantly (which the coach is already shouting about) or the yardstick is
    wrong.
  * **an unreachable verdict** — ``done`` is never awarded across a large
    enough sample of rated days. This is the signature f-cb50f53f named and it
    is the stricter of the two: a three-valued ladder whose top rung no real
    session can stand on is a two-valued ladder wearing a third label, exactly
    the "dead band" the card's gate refuses. Measured at the constant this
    check was written against: 0 of 19.

The asymmetry from the card's gate is kept deliberately. Concentration in
``done`` is NOT gated — an athlete who hits every session should read as
hitting every session, and #242 is the standing reminder that the failure this
project actually ships is a verdict that is too generous, which the evals pin
day by day rather than in aggregate.

NOT wired into CI, and cannot be: it needs a populated ``data/fitness.db`` that
CI does not have, and fabricating one would only ask the fixture whether it
agrees with itself. It is a MANUAL gate — run it before changing
``QUALITY_PACE_DONE_DEVIATION``, ``QUALITY_PACE_PARTIAL_DEVIATION`` or the
``GradingConfig`` fractions, and paste the output into the CHANGELOG entry and
the PR body.

Strictly READ-ONLY: the database is opened ``mode=ro`` through a URI, so a
write is refused by SQLite rather than merely avoided by convention.

Usage:
    uv run python scripts/calibrate_plan_verdicts.py
    uv run python scripts/calibrate_plan_verdicts.py --verbose
    uv run python scripts/calibrate_plan_verdicts.py --require-db

Exit codes: 0 clean (or skipped, no DB), 1 the distribution is degenerate,
2 the run could not be completed.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: Below this many rated days the histogram is noise and the gate abstains
#: rather than failing on thin data — the same reasoning as the card gate's
#: MIN_SAMPLE, scaled down because a quality day happens once or twice a week
#: while a card is graded per run.
MIN_SAMPLE = 12

#: Share of rated days on ``missed`` above which the yardstick, not the
#: athlete, is the likelier explanation. Same value as the card gate's
#: ``DEFAULT_MAX_FAIL_SHARE`` so the two surfaces call the same distribution
#: punitive.
DEFAULT_MAX_MISSED_SHARE = 0.60

#: The verdicts a quality day can reach once it has been rated. ``compliant``
#: is a rest-day verdict and never appears here.
VERDICTS = ("done", "partial", "missed")

#: Which constants govern the cut, so a failure names the thing to look at.
GOVERNING_CONSTANTS = (
    "plans.QUALITY_PACE_DONE_DEVIATION",
    "plans.QUALITY_PACE_PARTIAL_DEVIATION",
    "GradingConfig.done_fraction / .partial_fraction",
    "report_card.STAR_KNOTS / STAR_SCALE / STAR_NOISE / PLAN_TIGHTEN",
)


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection SQLite itself will refuse to write through.

    ``mode=ro`` is the point: the guarantee is enforced by the engine, not by
    this script remembering to only SELECT. Nate's live data is not ours.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def grade_quality_days(conn: sqlite3.Connection) -> list[dict]:
    """Regrade every quality day on every stored plan, oldest plan first.

    Every plan, not just the active one: the active plan carries eight quality
    days and a gate that abstains under MIN_SAMPLE would abstain forever on
    that alone. Archived and draft plans prescribe against the same activity
    history and exercise the same rubric, and a verdict is recomputed live on
    every read anyway (nothing stores one), so regrading them invents nothing.
    """
    from local_fitness import plans

    cfg = plans.GradingConfig()
    out: list[dict] = []
    plan_rows = conn.execute(
        "SELECT plan_id, status FROM training_plans ORDER BY plan_id"
    ).fetchall()
    for plan in plan_rows:
        workouts = [dict(r) for r in conn.execute(
            "SELECT * FROM plan_workouts WHERE plan_id = ? ORDER BY date, seq",
            (plan["plan_id"],),
        )]
        quality = [w for w in workouts if w.get("type") in plans._DURATION_TYPES]
        if not quality:
            continue
        # The production narrowing, not a whole-range fetch: this is the branch
        # every live caller takes, so it is the branch the gate must measure.
        by_date = plans.load_activities_by_date(
            workouts[0]["date"], workouts[-1]["date"], conn=conn,
            quality_dates=plans.quality_pace_dates(workouts),
        )
        for w in quality:
            acts = by_date.get(w["date"], [])
            rep_pace = plans._fastest_rep_pace(acts, cfg)
            target = w.get("target_pace_sec_per_km")
            out.append({
                "plan_id": plan["plan_id"],
                "status": plan["status"],
                "date": w["date"],
                "type": w["type"],
                "target_pace": target,
                "rep_pace": rep_pace,
                # Slow-side deviation, the quantity the cut is applied to.
                # None means the cap abstained and the day is not rated.
                "deviation": (
                    None if not (rep_pace and target)
                    else (rep_pace - target) / target
                ),
                "verdict": plans.classify_workout(w, acts, cfg),
            })
    return out


def format_report(
    rows: list[dict], *, max_missed_share: float, min_sample: int = MIN_SAMPLE,
) -> tuple[str, bool]:
    """Render the histogram and decide pass/fail. Pure — takes the graded rows
    and returns ``(text, failed)`` so a test can drive it without a database."""
    from local_fitness import plans

    rated = [r for r in rows if r["deviation"] is not None]
    counts = Counter(r["verdict"] for r in rated)
    out = [
        "quality-day verdict calibration",
        f"  {len(rows)} quality days across all plans; "
        f"{len(rated)} rated (a qualifying run with a rep-sized split)",
        f"  cuts: done <= {plans.QUALITY_PACE_DONE_DEVIATION:.4f} slow, "
        f"partial <= {plans.QUALITY_PACE_PARTIAL_DEVIATION:.4f} slow",
        "",
    ]
    if not rated:
        out.append("  no rated days — nothing to calibrate.")
        return "\n".join(out), False

    for v in VERDICTS:
        n = counts.get(v, 0)
        bar = "#" * n
        out.append(f"  {v:<8} {n:>3}  ({n / len(rated):>4.0%})  {bar}")
    devs = sorted(r["deviation"] for r in rated)
    out += [
        "",
        f"  deviation range {devs[0]:+.4f} .. {devs[-1]:+.4f}, "
        f"median {devs[len(devs) // 2]:+.4f}",
    ]

    failed = False
    if len(rated) < min_sample:
        out.append(f"\n  ABSTAINED — {len(rated)} rated days is under the "
                   f"{min_sample}-day floor; too thin to judge.")
        return "\n".join(out), False

    missed_share = counts.get("missed", 0) / len(rated)
    if missed_share > max_missed_share:
        failed = True
        out.append(f"\n  PUNITIVE SKEW — {missed_share:.0%} of rated days are "
                   f"`missed`, over the {max_missed_share:.0%} bar.")
    if not counts.get("done"):
        failed = True
        out.append("\n  UNREACHABLE VERDICT — `done` is never awarded across "
                   f"{len(rated)} rated days. A three-valued ladder whose top "
                   "rung no real session can stand on is a two-valued ladder.")
    if failed:
        out.append("  governing constants: " + ", ".join(GOVERNING_CONSTANTS))
    return "\n".join(out), failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=None,
                    help="database path (default: the app's resolved DB)")
    ap.add_argument("--max-missed-share", type=float,
                    default=DEFAULT_MAX_MISSED_SHARE,
                    help="fail above this share of rated days on `missed` "
                         "(default: 0.60)")
    ap.add_argument("--require-db", action="store_true",
                    help="treat a missing/empty database as a failure, not a skip")
    ap.add_argument("--verbose", action="store_true",
                    help="also list every quality day")
    args = ap.parse_args(argv)

    from local_fitness import db

    path = args.db or db.DEFAULT_DB_PATH
    if not Path(path).exists():
        print(f"SKIPPED — no database at {path}.\n"
              "This gate needs real plans and real activities; a fresh clone "
              "has nothing to calibrate against.")
        return 2 if args.require_db else 0

    # sqlite3.connect() is lazy — a file that is not a database opens fine and
    # only raises on first read, so the guard has to cover the queries too.
    try:
        conn = open_readonly(Path(path))
        try:
            rows = grade_quality_days(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"ERROR — could not read {path}: {exc}")
        return 2

    if not rows:
        print("SKIPPED — no quality days on any stored plan.")
        return 2 if args.require_db else 0

    if args.verbose:
        for r in rows:
            dev = "  n/a  " if r["deviation"] is None else f"{r['deviation']:+.4f}"
            print(f"plan {r['plan_id']} ({r['status']:<8}) {r['date']}  "
                  f"{r['type']:<8} dev={dev}  {r['verdict']}")
        print()

    text, failed = format_report(rows, max_missed_share=args.max_missed_share)
    print(text)
    if failed:
        print("\nFAIL — the verdict ladder has stopped discriminating. "
              "Recalibrate the named constants against this distribution "
              "before shipping.")
        return 1
    print("\nOK — the verdict ladder still uses every rung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
