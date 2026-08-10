#!/usr/bin/env python
"""Shadow-run the V2 brief generator and check structural parity vs the baseline.

The Phase-3 cutover GATE: before flipping ``LOCAL_FITNESS_BRIEF_V2`` on for the
live brief, prove the new toolless generator behaves like the old monolith. This
runs the V2 path across the same golden fixtures (with ``save=False`` so it can
never touch ``briefings/``), fingerprints each brief with
``ab_brief.extract_features``, and diffs those fingerprints against the committed
``tests/evals/baseline.json`` (version 2: captured on the V2 composer, with per-scenario invention rates).

The gate is **deterministic and judge-free** (per the design): structural parity
only. The structural checks per scenario are —
  * every brief is schema-valid (no parse/validation failures)
  * the mandated steps takeaway is present in every brief
  * takeaway count stays in [3, 5] AND within ±1 of the baseline's median
  * plan-folding matches plan_active (HARD for the plan scenario; ADVISORY for
    the others, because ab_brief's plan-keyword heuristic is noisy on the
    prompt's own "today's session" phrasing — the baseline itself recorded that)

Invention-rate is the OTHER half of the gate (0.58.0): each scenario's rate
must sit within ``_INVENTION_MARGIN`` of the committed baseline's recorded rate
(the version-2 baseline carries one per scenario). ``_INVENTION_BUDGET`` is
the rule only for a scenario the baseline has no rate for. A breach fails
parity and the exit code, same as a structural mismatch.

Cost discipline (the project's "quote spend + hard cap" rule), same as
capture_baseline.py: dry-run by DEFAULT; ``--run`` guarded by a hard cap;
``--mock`` aggregates canned V2 briefs with zero model calls; auth is the Claude
Max subscription (CLAUDE_CODE_OAUTH_TOKEN), no per-token billing.

Usage:
  uv run python scripts/shadow_run.py                       # dry-run: plan + estimate
  uv run python scripts/shadow_run.py --run                 # shadow V2 + parity report
  uv run python scripts/shadow_run.py --mock canned.json    # cost-free parity check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load `.env` from the project root so LOCAL_FITNESS_OPENCODE_AGENT (and any
# other .env-driven config) actually takes effect here — this script is one
# level shallower than src/local_fitness/cli.py, so parents[1] (not cli.py's
# parents[2]) reaches the repo root from this file's location.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import capture_baseline as cb
import eval_fixtures

_BASELINE_PATH = cb._BASELINE_PATH
_V2_ENV = "LOCAL_FITNESS_BRIEF_V2"
# Invention-rate gate (0.58.0 — was advisory-vs-constant while the baseline
# predated grounding). Per scenario: rate <= baseline_rate + _INVENTION_MARGIN.
# The margin absorbs run-to-run noise; the recorded baseline absorbs
# grounding's known per-scenario false positives (derived baselines,
# continuity recalls) — MEASURED on the 2026-08-10 v2 capture, two fixtures
# baseline at 0.83-0.88, so any absolute cap below that would make them
# permanently unpassable. _INVENTION_BUDGET therefore applies ONLY to a
# scenario the baseline has no rate for (there is no reference to be
# relative to), never as a cap under a recorded baseline.
_INVENTION_BUDGET = 0.5
_INVENTION_MARGIN = 0.15


def _median_count(fingerprints: list[dict]) -> int | None:
    if not fingerprints:
        return None
    counts = sorted(f["n_takeaways"] for f in fingerprints)
    return counts[len(counts) // 2]


def parity_report(baseline_doc: dict, shadow: dict[str, dict]) -> dict:
    """Compare V2 shadow records against the committed V1 baseline. Pure +
    deterministic so it is unit-tested without a model."""
    base = baseline_doc.get("scenarios", {})
    scenarios: dict[str, dict] = {}
    overall = True
    for name, rec in shadow.items():
        b_fps = base.get(name, {}).get("fingerprints", [])
        s_fps = rec.get("fingerprints", [])
        plan_active = name in cb._PLAN_ACTIVE_SCENARIOS
        warnings: list[str] = []

        checks = {
            "has_success": bool(s_fps),
            "schema_valid": rec.get("schema_invalid", 0) == 0 and bool(s_fps),
            "steps_mandate": bool(s_fps) and all(f["has_steps"] for f in s_fps),
            "count_in_range": bool(s_fps) and all(3 <= f["n_takeaways"] <= 5 for f in s_fps),
        }
        b_med, s_med = _median_count(b_fps), _median_count(s_fps)
        checks["count_near_baseline"] = (
            b_med is not None and s_med is not None and abs(s_med - b_med) <= 1)

        plan_ok = bool(s_fps) and all(f["mentions_plan"] == plan_active for f in s_fps)
        if plan_active:
            checks["plan_parity"] = plan_ok          # HARD: the plan MUST fold in
        else:
            checks["plan_parity"] = True             # not gated (heuristic noise)
            if not plan_ok:
                warnings.append(
                    "mentions_plan flipped on a non-plan scenario — ab_brief "
                    "plan-keyword noise on 'today's session' phrasing (advisory)")

        # Invention-rate GATE (0.58.0): within margin of the baseline's own
        # recorded rate, bounded by the absolute budget. Unscored (mock path,
        # rate None) leaves the check passing — the mock path has no context
        # to score against, and structural parity is still gated.
        inv = rec.get("invention_rate")
        base_inv = base.get(name, {}).get("invention_rate")
        if inv is None:
            checks["invention_rate"] = True
            warnings.append("invention_rate not scored (no context on this path)")
        else:
            ceiling = (
                base_inv + _INVENTION_MARGIN
                if base_inv is not None else _INVENTION_BUDGET
            )
            checks["invention_rate"] = inv <= ceiling
            if not checks["invention_rate"]:
                warnings.append(
                    f"invention_rate {inv} > ceiling {round(ceiling, 3)} "
                    f"(baseline {base_inv}, margin {_INVENTION_MARGIN}, "
                    f"budget {_INVENTION_BUDGET})")

        parity = all(checks.values())
        overall = overall and parity
        scenarios[name] = {
            "parity": parity,
            "checks": checks,
            "warnings": warnings,
            "baseline_median_count": b_med,
            "shadow_median_count": s_med,
            "invention_rate": inv,
            "flakes": len(rec.get("flakes", [])),
        }
    scored = any(s["invention_rate"] is not None for s in scenarios.values())
    gate = (f"GATED: per-scenario <= baseline + {_INVENTION_MARGIN} "
            f"(budget {_INVENTION_BUDGET} only when the baseline has no rate)"
            if scored
            else "not scored on this path (mock) — structural parity only")
    return {
        "overall_parity": overall,
        "invention_rate_gate": gate,
        "scenarios": scenarios,
    }


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _capture_v2(scenarios: list[str], model: str, runs: int) -> dict[str, dict]:
    """Live: force V2 ON, run the generator per fixture (save=False), and record
    structural fingerprints PLUS the grounding invention-rate per scenario.

    Glue over the LLM composer (not unit-tested — a test would only assert a mock
    replays itself); the parity verdict it feeds (parity_report) is unit-tested.
    """
    import asyncio
    import tempfile

    from local_fitness import db
    from local_fitness.agent import brief_planner, briefing, briefs, grounding

    # NOTE: LOCAL_FITNESS_BRIEFINGS_DIR is set below for completeness/back-compat,
    # but it is NOT sufficient on its own — briefs.DEFAULT_BRIEFINGS_DIR is
    # resolved ONCE at module-import time, and `briefing` (imported above) has
    # already pulled `briefs` in by the time this function runs, so the env var
    # override is a no-op. Without the direct attribute mutation below, every
    # "fixture-only" shadow-run was silently folding real recent-brief content
    # (real personal coaching history) into the prompt via
    # briefing._recent_briefs_summary() -> briefs.DEFAULT_BRIEFINGS_DIR. Fixed
    # the same way db.DEFAULT_DB_PATH is already handled two lines below.
    keys = (_V2_ENV, "LOCAL_FITNESS_NOTES_PATH", "LOCAL_FITNESS_BRIEFINGS_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    orig_db = db.DEFAULT_DB_PATH
    orig_briefings_dir = briefs.DEFAULT_BRIEFINGS_DIR
    out: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ[_V2_ENV] = "1"
            os.environ["LOCAL_FITNESS_NOTES_PATH"] = str(root / "notes.md")
            os.environ["LOCAL_FITNESS_BRIEFINGS_DIR"] = str(root / "briefings")
            briefs.DEFAULT_BRIEFINGS_DIR = root / "briefings"
            for scenario in scenarios:
                fixture = eval_fixtures.build_fixture_db(scenario, root / scenario / "fitness.db")
                db.DEFAULT_DB_PATH = fixture
                # Same context the V2 generator assembles internally (same DB +
                # today) → invention_rate is scored against the right pool.
                context = brief_planner.assemble_brief_context()
                results: list[dict] = []
                rates: list[float] = []
                for _ in range(runs):
                    try:
                        brief = asyncio.run(briefing._generate(model=model, save=False))
                        results.append(brief.model_dump())
                        rates.append(grounding.invention_rate(brief, context))
                    except Exception as e:  # noqa: BLE001 — one bad gen ≠ abort
                        # Alt-model failures (e.g. a misconfigured opencode
                        # agent) propagate with NO log emitted anywhere else
                        # in that path — this print is the only place the
                        # actionable remediation text ever reaches the user.
                        print(f"  FAILED: {str(e).splitlines()[0]}")
                        results.append({"error": str(e)})
                rec = cb.aggregate_scenario(
                    results, plan_active=scenario in cb._PLAN_ACTIVE_SCENARIOS)
                rec["invention_rate"] = round(sum(rates) / len(rates), 3) if rates else None
                out[scenario] = rec
                print(f"  {scenario}: {rec['schema_valid']}/{rec['runs']} valid, "
                      f"inv_rate={rec['invention_rate']}, "
                      f"consistent={rec['consistency']['consistent']}")
    finally:
        db.DEFAULT_DB_PATH = orig_db
        briefs.DEFAULT_BRIEFINGS_DIR = orig_briefings_dir
        _restore_env(saved)
    return out


def _print_report(report: dict) -> None:
    print("\n=== V2 shadow-run structural parity vs baseline ===")
    for name, rec in report["scenarios"].items():
        verdict = "PARITY" if rec["parity"] else "MISMATCH"
        failed = [k for k, v in rec["checks"].items() if not v]
        print(f"  {name}: {verdict}  "
              f"(baseline_count={rec['baseline_median_count']} "
              f"shadow_count={rec['shadow_median_count']} "
              f"inv_rate={rec['invention_rate']} flakes={rec['flakes']})")
        for f in failed:
            print(f"      FAILED CHECK: {f}")
        for w in rec["warnings"]:
            print(f"      warning: {w}")
    print(f"\n  invention-rate gate: {report['invention_rate_gate']}")
    if report["overall_parity"]:
        print("\nOVERALL: PARITY HOLDS (structure + invention rate).")
    else:
        print("\nOVERALL: PARITY FAILED — keep the flag OFF; investigate the "
              "mismatched scenarios before retry.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Shadow-run V2 and check parity vs baseline")
    ap.add_argument("--scenarios", default=",".join(eval_fixtures.SCENARIOS))
    ap.add_argument("--runs", type=int, default=cb.DEFAULT_RUNS)
    ap.add_argument("--model", default=cb.DEFAULT_MODEL)
    ap.add_argument("--baseline", default=str(_BASELINE_PATH))
    ap.add_argument("--run", action="store_true", help="call the model (default: dry-run)")
    ap.add_argument("--mock", help="JSON {scenario: [v2_brief, ...]} — parity check, no model")
    ap.add_argument("--out", help="optional path to write the parity report JSON")
    args = ap.parse_args(argv)
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in eval_fixtures.SCENARIOS]
    if unknown:
        print(f"REFUSED: unknown scenario(s) {unknown}; "
              f"valid: {list(eval_fixtures.SCENARIOS)}", file=sys.stderr)
        return 2

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"REFUSED: baseline not found at {baseline_path}. Run "
              "capture_baseline.py --run first.", file=sys.stderr)
        return 2
    baseline_doc = json.loads(baseline_path.read_text())

    if args.mock:
        shadow = cb._aggregate_mock(json.loads(Path(args.mock).read_text()))
        report = parity_report(baseline_doc, shadow)
        _print_report(report)
        _maybe_write(args.out, report)
        return 0 if report["overall_parity"] else 1

    est = cb.estimate(scenarios, args.runs)
    from local_fitness.agent.briefing import _ALT_MODEL_PREFIX

    is_alt_model = args.model.startswith(_ALT_MODEL_PREFIX)
    provider = (
        args.model[len(_ALT_MODEL_PREFIX):].partition("/")[0] if is_alt_model else None
    )
    if not args.run:
        print(f"Shadow-run plan: scenarios={scenarios} runs={args.runs} (V2 flag forced ON)")
        print(f"  -> {est['generations']} generations (hard cap {cb.MAX_GENERATIONS})")
        if provider == "ollama":
            print("  -> cost: $0 (local via Ollama). Wall-clock: unmeasured for this "
                  "model — the estimate below is Claude-derived and does not apply; "
                  "the first run calibrates it.")
        elif is_alt_model:
            print(f"  -> cost: network call via opencode gateway ({args.model[len(_ALT_MODEL_PREFIX):]}) "
                  "— cost depends on the model, not necessarily free. Wall-clock: "
                  "unmeasured for this model — the estimate below is Claude-derived "
                  "and does not apply; the first run calibrates it.")
        else:
            print(f"  -> est ~{est['est_seconds']}s wall, ~{est['est_output_tokens']:,} output tokens")
        print(f"  Compares V2 fingerprints against {baseline_path}.")
        if provider == "ollama":
            print("  Runs locally via Ollama — no subscription/API cost.")
        elif is_alt_model:
            print("  Not local — leaves the machine via opencode's hosted gateway.")
        else:
            print("  Uses the Claude Max subscription (CLAUDE_CODE_OAUTH_TOKEN) — no per-token billing.")
        print("  Re-run with --run to execute, or --mock <file> for a cost-free check.")
        return 0

    if est["generations"] > cb.MAX_GENERATIONS:
        print(f"REFUSED: {est['generations']} generations exceeds cap "
              f"{cb.MAX_GENERATIONS}. Lower --runs or --scenarios.", file=sys.stderr)
        return 2

    print(f"Shadow-running V2: {est['generations']} generations (save=False)...")
    shadow = _capture_v2(scenarios, args.model, args.runs)
    report = parity_report(baseline_doc, shadow)
    _print_report(report)
    _maybe_write(args.out, report)
    return 0 if report["overall_parity"] else 1


def _maybe_write(out: str | None, report: dict) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"\nWrote parity report -> {out}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
