#!/usr/bin/env python
"""A/B simulation harness for prompt changes.

Generate the daily brief across models and check the STRUCTURED output stays
consistent — run this whenever `agent/prompts.py` changes so a wording tweak
can't silently shift brief content/tone/schema across models.

Briefs are non-deterministic LLM output, so this does NOT diff text. It
extracts structural features (takeaway count, the mandated steps takeaway,
tones, whether an active training plan is folded in) and flags divergences
across models and repeated runs.

Cost discipline (per the project's "quote spend + hard cap" rule):
  * Dry-run by DEFAULT — prints the plan + estimate and exits, no model calls.
  * `--run` actually generates, but a hard cap (MAX_GENERATIONS) refuses large
    fan-outs.
  * `--mock <file>` runs the whole comparison on canned briefs with zero model
    calls (used by the unit test and for CI).

Usage:
  uv run python scripts/ab_brief.py                      # dry-run: show the plan + estimate
  uv run python scripts/ab_brief.py --run                # generate sonnet+opus x2 and compare
  uv run python scripts/ab_brief.py --run --runs 1 --models claude-sonnet-4-6
  uv run python scripts/ab_brief.py --mock fixtures.json # cost-free comparison
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter

MAX_GENERATIONS = 8
EST_SECONDS_PER_BRIEF = 25
EST_OUTPUT_TOKENS_PER_BRIEF = 40_000
DEFAULT_MODELS = ["claude-sonnet-4-6", "claude-opus-4-7"]

_STEPS_KEYWORDS = ("step",)
_PLAN_KEYWORDS = (
    "training plan", "adherence", "prescribed", "today's session",
    "goal pace", "race day", "race pace", "on pace for", "plan calls for",
)


def extract_features(brief: dict) -> dict:
    """Structural fingerprint of one brief (model-agnostic, text-free)."""
    tks = brief.get("takeaways", [])
    blob = " ".join(
        f"{t.get('headline', '')} {t.get('summary', '')} {t.get('details', '')}".lower()
        for t in tks
    )
    return {
        "n_takeaways": len(tks),
        "tones": sorted(Counter(t.get("tone", "neutral") for t in tks).items()),
        "has_steps": any(k in blob for k in _STEPS_KEYWORDS),
        "mentions_plan": any(k in blob for k in _PLAN_KEYWORDS),
        "metrics": sorted({t["metric"]["metric"] for t in tks if t.get("metric")}),
    }


def compare(features_by_label: dict[str, dict], plan_active: bool) -> dict:
    """Flag divergences across briefs. Returns {consistent, divergences}."""
    feats = features_by_label
    divergences: list[str] = []

    missing_steps = [lbl for lbl, f in feats.items() if not f["has_steps"]]
    if missing_steps:
        divergences.append(f"mandated steps takeaway missing in: {missing_steps}")

    counts = {lbl: f["n_takeaways"] for lbl, f in feats.items()}
    if any(c < 3 or c > 5 for c in counts.values()):
        divergences.append(f"takeaway count outside [3,5]: {counts}")
    if counts and max(counts.values()) - min(counts.values()) > 1:
        divergences.append(f"takeaway count varies by >1 across runs: {counts}")

    if plan_active:
        not_folded = [lbl for lbl, f in feats.items() if not f["mentions_plan"]]
        if not_folded:
            divergences.append(f"active plan NOT folded into brief in: {not_folded}")
    else:
        leaked = [lbl for lbl, f in feats.items() if f["mentions_plan"]]
        if leaked:
            divergences.append(f"plan content present with NO active plan in: {leaked}")

    return {"consistent": not divergences, "divergences": divergences}


def estimate(models: list[str], runs: int) -> dict:
    gens = len(models) * runs
    return {
        "generations": gens,
        "est_seconds": gens * EST_SECONDS_PER_BRIEF,
        "est_output_tokens": gens * EST_OUTPUT_TOKENS_PER_BRIEF,
    }


async def _generate_features(models: list[str], runs: int) -> dict[str, dict]:
    from local_fitness.agent import briefing

    out: dict[str, dict] = {}
    for model in models:
        for r in range(runs):
            brief = await briefing._generate(model=model)
            out[f"{model}#{r + 1}"] = extract_features(brief.model_dump())
    return out


def _plan_active() -> bool:
    from local_fitness import plans

    return plans.get_active_plan() is not None


def _report(feats: dict[str, dict], result: dict) -> None:
    print("\n=== A/B brief feature comparison ===")
    for label, f in feats.items():
        print(f"  {label}: takeaways={f['n_takeaways']} steps={f['has_steps']} "
              f"plan={f['mentions_plan']} tones={f['tones']} metrics={f['metrics']}")
    if result["consistent"]:
        print("\nCONSISTENT: no divergences across models/runs.")
    else:
        print("\nDIVERGENCES:")
        for d in result["divergences"]:
            print(f"  - {d}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A/B brief simulation across models")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--runs", type=int, default=2, help="generations per model")
    ap.add_argument("--run", action="store_true", help="actually call the models (default: dry-run)")
    ap.add_argument("--mock", help="JSON fixture {plan_active, briefs:{label: brief}} — no model calls")
    args = ap.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if args.mock:
        with open(args.mock) as fh:
            data = json.load(fh)
        feats = {label: extract_features(b) for label, b in data["briefs"].items()}
        result = compare(feats, bool(data.get("plan_active", False)))
        _report(feats, result)
        return 0 if result["consistent"] else 1

    est = estimate(models, args.runs)
    if not args.run:
        print(f"A/B brief plan: models={models} runs={args.runs}")
        print(f"  -> {est['generations']} generations (hard cap {MAX_GENERATIONS})")
        print(f"  -> est ~{est['est_seconds']}s wall, ~{est['est_output_tokens']:,} output tokens")
        print("  Uses the Claude Max subscription (CLAUDE_CODE_OAUTH_TOKEN) — no per-token API billing.")
        print("  Re-run with --run to execute, or --mock <file> for a cost-free check.")
        return 0

    if est["generations"] > MAX_GENERATIONS:
        print(f"REFUSED: {est['generations']} generations exceeds cap {MAX_GENERATIONS}. "
              "Lower --runs or --models.", file=sys.stderr)
        return 2

    feats = asyncio.run(_generate_features(models, args.runs))
    result = compare(feats, _plan_active())
    _report(feats, result)
    return 0 if result["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
