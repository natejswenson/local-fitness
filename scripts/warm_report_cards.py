"""Re-render stored report cards so their coach reads are cached again.

WHY THIS EXISTS
---------------
Every stored card doubles as a per-activity read cache: a re-render whose
prompt key matches the stored row reuses the read with no SDK call. Measured
end to end on the live corpus — a MISS costs **14.5 s**, a HIT **0.003 s**.

The catch is that the key covers the whole prompt, so any release that changes
the rubric or the read prompt legitimately invalidates every stored card at
once. Measured 2026-08-03: **1 of 15** cards still matched, purely from the
0.41–0.43 rubric work (continuity, the Stimulus block, the hindsight/foresight
sections, bounded displays). Nothing was broken — the cards were simply stale,
and the cost landed on Nate the next time he opened one.

This script pays that cost ONCE, deliberately, off the request path. Run it
after a release that touches ``report_card.py`` or ``workout_coach.py``.

It is NOT a backfill. It only re-renders cards that already exist; history
still accumulates as cards render for the first time.

COST
----
Each stale card is one Claude call (~10 s, Sonnet tier, ``effort="low"``).
``--dry-run`` is the default and is FREE — it drives the real render path with
the SDK call and the DB write stubbed out, so it reports exactly which cards
would regenerate without spending anything or touching a row. Nothing is
spent until you pass ``--yes``, and ``--max-calls`` is a hard pre-call gate,
not a warning.

Reuses the production path (``workout_report_card``'s own handler) rather than
reimplementing the key computation — the same rule
``calibrate_report_card.py`` follows. A reimplementation would drift from the
thing it is meant to warm, which is exactly the bug this script exists to
paper over.

Usage:
    uv run python scripts/warm_report_cards.py                  # free survey
    uv run python scripts/warm_report_cards.py --yes            # warm them
    uv run python scripts/warm_report_cards.py --yes --limit 5
    uv run python scripts/warm_report_cards.py --days 90 --yes

Exit codes: 0 done (or nothing stale), 1 a render failed, 2 could not run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_fitness import db  # noqa: E402
from local_fitness.agent import card_store, tools, workout_coach  # noqa: E402

LOG = logging.getLogger("warm_report_cards")

#: Belt-and-braces ceiling. The live corpus is ~15 cards; a run asking for
#: hundreds means a wrong --days or a misread DB, and should stop rather than
#: quietly spend. Overridable with --max-calls, never silently exceeded.
DEFAULT_MAX_CALLS = 40

#: Measured 2026-08-03 over real generations (Sonnet, effort=low, thinking
#: disabled): median 10.0 s, max 10.8 s. Used only to quote wall-clock up front.
SECONDS_PER_CALL = 10.0


class _SkipGeneration(Exception):
    """Sentinel for --dry-run: raised in place of the SDK call.

    The handler catches a failed generation, falls back to the deterministic
    template and sets ``read_key = None`` — which by ``save_card``'s documented
    contract can never overwrite a real-read row. We stub the save out anyway,
    so a dry run is inert twice over.
    """


def _stale_marker(activity_id: int, before: str | None, after: str | None) -> bool:
    """Did this render regenerate the read? True when the stored key moved."""
    return before != after


async def _render(activity_id: int) -> dict:
    """Drive the real tool handler for one card."""
    result = await tools.workout_report_card.handler(
        {"activity_id": activity_id, "format": "table"})
    return result


def _stored_key(activity_id: int) -> str | None:
    row = card_store.load_read(activity_id)
    return row[0] if row else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=365,
                    help="only cards for activities in the trailing N days (default 365)")
    ap.add_argument("--limit", type=int, default=200,
                    help="most cards to consider, newest run first (default 200)")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                    help=f"hard pre-call ceiling on SDK generations (default {DEFAULT_MAX_CALLS})")
    ap.add_argument("--yes", action="store_true",
                    help="actually spend: without this the run is a free survey")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    db_path = db.get_db_path()
    if not Path(db_path).exists():
        print(f"No database at {db_path} — nothing to warm.")
        return 0

    # Migrate before reading. This script is the FIRST thing a release runs
    # (CLAUDE.md: "ship the warm WITH the release"), so it is also the first
    # thing to meet a schema that moved — 0.50.0's `overall_stars` columns made
    # that concrete, with `list_cards` raising OperationalError before a single
    # card was surveyed. Every other entry point (`cli.py`, `web/server.py`)
    # already inits; this one bypassed them. Idempotent, so it costs nothing
    # when the schema is already current.
    db.init_schema()

    from datetime import date, timedelta
    start = (date.today() - timedelta(days=args.days)).isoformat()
    cards = card_store.list_cards(start_date=start, limit=args.limit)
    if not cards:
        print(f"No stored cards in the last {args.days} days.")
        return 0

    print(f"Stored cards in window: {len(cards)}  (db: {db_path})")

    # --- pass 1: which cards are stale? Always free. -------------------------
    # The survey drives the REAL handler with the two expensive/irreversible
    # steps stubbed: the SDK call and the row write. Whether the handler
    # reaches the generation stub IS the answer — a key match short-circuits
    # before it. Nothing here re-derives the cache key, so this cannot drift
    # from the path it is measuring.
    real_generate = workout_coach.generate_read_cached
    real_save = card_store.save_card
    reached_generation = False

    async def _no_generate(*_a, **_kw):
        nonlocal reached_generation
        reached_generation = True
        raise _SkipGeneration

    def _no_save(*_a, **_kw):
        return None

    workout_coach.generate_read_cached = _no_generate
    card_store.save_card = _no_save
    # The survey deliberately trips the handler's fail-silent generation
    # branch, which logs a warning + traceback every time. That is correct
    # behaviour for a real failure and pure noise here, so the tools logger is
    # quietened for the survey only — restored in the finally below, so a real
    # warm run still reports everything.
    _tools_log = logging.getLogger("local_fitness.agent.tools")
    _prior_level = _tools_log.level
    if not args.verbose:
        _tools_log.setLevel(logging.CRITICAL)
    stale: list[dict] = []
    unreadable: list[dict] = []
    try:
        for c in cards:
            aid = c["activity_id"]
            reached_generation = False
            errored = False
            try:
                result = asyncio.run(_render(aid))
                # The handler returns an _err ENVELOPE rather than raising, so
                # a card whose activity can no longer be loaded would otherwise
                # look "warm" — silently under-quoting the run and skipping the
                # card that most needs a look.
                errored = bool(result.get("is_error"))
            except Exception:
                errored = True
                LOG.info("survey render failed for %s", aid, exc_info=True)
            if errored:
                # Not evidence of a warm cache. Count it as needing attention
                # so --yes surfaces the real error instead of skipping it.
                unreadable.append(c)
                stale.append(c)
            elif reached_generation:
                stale.append(c)
    finally:
        workout_coach.generate_read_cached = real_generate
        card_store.save_card = real_save
        _tools_log.setLevel(_prior_level)

    if not stale:
        print("All stored cards are warm — no SDK calls needed.")
        return 0

    n = len(stale)
    print(f"\nStale (would regenerate): {n} of {len(cards)}")
    if unreadable:
        print(f"  ({len(unreadable)} of those could not be rendered at all — "
              f"the activity may no longer be loadable; --yes will show why)")
    for c in stale[:20]:
        print(f"  {c['activity_date']}  id={c['activity_id']}  "
              f"{c.get('overall_grade') or '?'}")
    if n > 20:
        print(f"  … and {n - 20} more")

    est_s = n * SECONDS_PER_CALL
    print(f"\nEstimated cost: {n} Claude call(s), ~{est_s / 60:.1f} min wall clock.")
    print("Each miss otherwise costs 14.5 s the next time that card is opened.")

    if n > args.max_calls:
        print(f"\nREFUSING: {n} exceeds --max-calls={args.max_calls}. "
              f"Narrow with --days/--limit, or raise the ceiling deliberately.")
        return 2

    if not args.yes:
        print("\nDry run — nothing spent, nothing written. Re-run with --yes to warm.")
        return 0

    # --- pass 2: actually warm ----------------------------------------------
    print()
    failures = 0
    t0 = time.monotonic()
    for i, c in enumerate(stale, 1):
        aid = c["activity_id"]
        before = _stored_key(aid)
        t = time.monotonic()
        try:
            asyncio.run(_render(aid))
        except Exception:
            failures += 1
            LOG.warning("render failed for %s", aid, exc_info=True)
            print(f"  [{i}/{n}] {c['activity_date']} id={aid}  FAILED")
            continue
        after = _stored_key(aid)
        moved = _stale_marker(aid, before, after)
        print(f"  [{i}/{n}] {c['activity_date']} id={aid}  "
              f"{time.monotonic() - t:.1f}s  {'regenerated' if moved else 'unchanged'}")

    print(f"\nDone in {time.monotonic() - t0:.0f}s. "
          f"{n - failures} warmed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
