#!/usr/bin/env python
"""Capture the brief eval BASELINE on the CURRENT prompt.

Phase-1 of the agent/code-separation build: before any prompt shrink, freeze a
**deterministic structural fingerprint** of how today's monolithic prompt behaves
across the golden fixtures (`eval_fixtures.SCENARIOS`). Phase-3's shadow-run then
diffs the new toolless generator against this committed baseline and only flips
``LOCAL_FITNESS_BRIEF_V2`` when structural parity holds.

The baseline is **structure only** (via `ab_brief.extract_features`) — takeaway
count, tones, the mandated steps takeaway, plan-folding, charted metrics, schema
validity, and a flake rate. It is text-free and judge-free (the LLM-judge is
deferred/nightly per the design). The invention-rate column is intentionally
absent here: it needs `grounding.flag`, which lands in Phase 4 and backfills it.

Cost discipline (the project's "quote spend + hard cap" rule):
  * Dry-run by DEFAULT — prints the plan + estimate and exits, no model calls.
  * `--run` actually generates, guarded by a hard cap (MAX_GENERATIONS).
  * `--mock <file>` aggregates canned briefs with ZERO model calls (CI / the unit
    test path). File shape: ``{"<scenario>": [<brief dict>, ...], ...}``.
  * Auth is the Claude Max subscription (CLAUDE_CODE_OAUTH_TOKEN) — no per-token
    API billing; runs draw on the subscription's rate budget.

Each generation runs with ``save=False`` so it can NEVER overwrite the live
``briefings/<date>.json``; a generation that fails to parse is recorded as a
flake (surfaced as a flake rate) rather than aborting the capture.

Usage:
  uv run python scripts/capture_baseline.py                 # dry-run: plan + estimate
  uv run python scripts/capture_baseline.py --run           # capture + write baseline.json
  uv run python scripts/capture_baseline.py --run --runs 1 --scenarios green_light,sparse
  uv run python scripts/capture_baseline.py --mock canned.json --out /tmp/b.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import ab_brief  # scripts/ is on sys.path via tests/conftest.py and __main__ below
import eval_fixtures

# 6 scenarios x 2 runs = 12 generations sits under this cap; raising --runs past
# it is refused so a fat fan-out can't run away.
MAX_GENERATIONS = 16
DEFAULT_RUNS = 2
DEFAULT_MODEL = "claude-sonnet-4-6"
_PLAN_ACTIVE_SCENARIOS = frozenset({"taper_plan"})
_BASELINE_PATH = Path(__file__).resolve().parents[1] / "tests" / "evals" / "baseline.json"


def _schema_valid(brief: dict) -> bool:
    """True iff ``brief`` round-trips through the production ``Brief`` schema."""
    from local_fitness.agent.schemas import Brief
    from pydantic import ValidationError

    try:
        Brief.model_validate(brief)
        return True
    except ValidationError:
        return False


def aggregate_scenario(results: list[dict], plan_active: bool) -> dict:
    """Reduce a scenario's per-run outputs to its baseline record.

    ``results`` is a list where each item is either a brief dict or an error
    marker ``{"error": "..."}`` (a generation that failed to parse). Pure +
    deterministic so it is unit-tested without a model.
    """
    fingerprints: list[dict] = []
    flakes: list[str] = []
    schema_valid = 0
    schema_invalid = 0
    for item in results:
        if "error" in item:
            # Record only the error REASON (first line, capped). The composer's
            # parse error embeds the full raw model response after a blank line;
            # that prose (even fabricated-fixture prose) doesn't belong in a
            # committed structural baseline.
            flakes.append(item["error"].split("\n", 1)[0].strip()[:200])
            continue
        if _schema_valid(item):
            schema_valid += 1
        else:
            schema_invalid += 1
        fingerprints.append(ab_brief.extract_features(item))

    # Reuse the A/B structural-consistency check over the successful runs.
    feats_by_label = {f"run#{i + 1}": fp for i, fp in enumerate(fingerprints)}
    consistency = (
        ab_brief.compare(feats_by_label, plan_active)
        if feats_by_label
        else {"consistent": False, "divergences": ["no successful generations"], "failures": {}}
    )
    return {
        "plan_active": plan_active,
        "runs": len(results),
        "schema_valid": schema_valid,
        "schema_invalid": schema_invalid,
        "flakes": flakes,
        "fingerprints": fingerprints,
        "consistency": {
            "consistent": consistency["consistent"],
            "divergences": consistency["divergences"],
        },
    }


def estimate(scenarios: list[str], runs: int) -> dict:
    gens = len(scenarios) * runs
    return {
        "generations": gens,
        "est_seconds": gens * ab_brief.EST_SECONDS_PER_BRIEF,
        "est_output_tokens": gens * ab_brief.EST_OUTPUT_TOKENS_PER_BRIEF,
    }


async def _generate_one(model: str) -> dict:
    """Drain one live brief generation into a dict, or an error marker."""
    from local_fitness.agent import briefing

    try:
        brief = await briefing._generate(model=model, save=False)
        return brief.model_dump()
    except Exception as e:  # noqa: BLE001 — one bad generation must not abort the capture
        return {"error": str(e)}


def _capture_live(scenarios: list[str], model: str, runs: int) -> dict[str, dict]:
    """Run the live composer against each fixture and aggregate. Mutates the
    process DB pointer + isolation env per scenario; restores them after."""
    from local_fitness import db

    orig_db = db.DEFAULT_DB_PATH
    orig_notes = os.environ.get("LOCAL_FITNESS_NOTES_PATH")
    orig_briefs = os.environ.get("LOCAL_FITNESS_BRIEFINGS_DIR")
    out: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            # Empty, isolated notes + briefings so saved user notes and prior
            # briefs can't leak into the fixture's brief (controlled continuity).
            os.environ["LOCAL_FITNESS_NOTES_PATH"] = str(tmp_root / "user_notes.md")
            os.environ["LOCAL_FITNESS_BRIEFINGS_DIR"] = str(tmp_root / "briefings")
            for scenario in scenarios:
                fixture = eval_fixtures.build_fixture_db(
                    scenario, tmp_root / scenario / "fitness.db"
                )
                db.DEFAULT_DB_PATH = fixture
                results = [asyncio.run(_generate_one(model)) for _ in range(runs)]
                out[scenario] = aggregate_scenario(
                    results, plan_active=scenario in _PLAN_ACTIVE_SCENARIOS
                )
                rec = out[scenario]
                print(
                    f"  {scenario}: {rec['schema_valid']}/{rec['runs']} valid, "
                    f"{len(rec['flakes'])} flake(s), "
                    f"consistent={rec['consistency']['consistent']}"
                )
    finally:
        db.DEFAULT_DB_PATH = orig_db
        _restore_env("LOCAL_FITNESS_NOTES_PATH", orig_notes)
        _restore_env("LOCAL_FITNESS_BRIEFINGS_DIR", orig_briefs)
    return out


def _restore_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def _aggregate_mock(data: dict[str, list[dict]]) -> dict[str, dict]:
    """Aggregate canned briefs (no model calls) — the CI / unit-test path."""
    out: dict[str, dict] = {}
    for scenario, briefs in data.items():
        out[scenario] = aggregate_scenario(
            list(briefs), plan_active=scenario in _PLAN_ACTIVE_SCENARIOS
        )
    return out


def build_baseline(scenarios: dict[str, dict], *, model: str, runs: int,
                   captured_at: str) -> dict:
    """Assemble the committed baseline document from per-scenario records."""
    return {
        "version": 1,
        "captured_at": captured_at,
        "model": model,
        "runs_per_scenario": runs,
        "note": (
            "Structural baseline of the CURRENT (pre-shrink) brief prompt. "
            "Phase-3 shadow-run must hold structural parity vs this before the "
            "LOCAL_FITNESS_BRIEF_V2 cutover. Invention-rate is backfilled in "
            "Phase 4 (needs grounding.flag)."
        ),
        "scenarios": scenarios,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture the brief eval baseline")
    ap.add_argument("--scenarios", default=",".join(eval_fixtures.SCENARIOS),
                    help="comma-separated subset of fixture scenarios")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help="generations per scenario (distribution width)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--run", action="store_true",
                    help="actually call the model (default: dry-run plan + estimate)")
    ap.add_argument("--mock", help="JSON {scenario: [brief, ...]} — aggregate with no model calls")
    ap.add_argument("--out", default=str(_BASELINE_PATH),
                    help="where to write the baseline JSON")
    args = ap.parse_args(argv)
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in eval_fixtures.SCENARIOS]
    if unknown:
        print(f"REFUSED: unknown scenario(s) {unknown}; "
              f"valid: {list(eval_fixtures.SCENARIOS)}", file=sys.stderr)
        return 2

    if args.mock:
        with open(args.mock) as fh:
            data = json.load(fh)
        records = _aggregate_mock(data)
        doc = build_baseline(records, model="mock", runs=0,
                             captured_at="mock")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        print(f"Wrote mock baseline -> {args.out}")
        return 0

    est = estimate(scenarios, args.runs)
    if not args.run:
        print(f"Baseline capture plan: scenarios={scenarios} runs={args.runs}")
        print(f"  -> {est['generations']} generations (hard cap {MAX_GENERATIONS})")
        print(f"  -> est ~{est['est_seconds']}s wall, ~{est['est_output_tokens']:,} output tokens")
        print("  Uses the Claude Max subscription (CLAUDE_CODE_OAUTH_TOKEN) — no per-token billing.")
        print(f"  Writes -> {args.out}")
        print("  Re-run with --run to execute, or --mock <file> for a cost-free aggregation.")
        return 0

    if est["generations"] > MAX_GENERATIONS:
        print(f"REFUSED: {est['generations']} generations exceeds cap {MAX_GENERATIONS}. "
              "Lower --runs or --scenarios.", file=sys.stderr)
        return 2

    print(f"Capturing baseline: {est['generations']} generations "
          f"(model={args.model}, save=False)...")
    records = _capture_live(scenarios, args.model, args.runs)
    doc = build_baseline(records, model=args.model, runs=args.runs,
                        captured_at=datetime.now().isoformat(timespec="seconds"))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    total_flakes = sum(len(r["flakes"]) for r in records.values())
    print(f"Wrote baseline -> {args.out}  ({total_flakes} flake(s) across the run)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
