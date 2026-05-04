"""Generate Sonnet + Haiku briefs against the same DB state and score both.

This is the automated alternative to "eyeball three briefs" — it produces a
side-by-side rubric report that informs the decision: should ``DEFAULT_MODEL``
flip from claude-sonnet-4-6 to claude-haiku-4-5?

Generated briefs are written to ``/tmp/brief_eval_{model}.json`` so they
don't clobber the live ``briefings/<date>.json``.

Usage::

    uv run python tools/scorer/compare_models.py
    uv run python tools/scorer/compare_models.py --models sonnet,haiku  # default
    uv run python tools/scorer/compare_models.py --models sonnet,sonnet  # noise floor

Cost estimate per run: ~$0.10-0.30 per brief generation (model-dependent) +
~$0.05 per scoring call. Two briefs + two scores ≈ $0.40 total.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools" / "scorer"))

from local_fitness.agent import briefing as briefing_mod  # noqa: E402

# Reuse the scorer's judge + data-snapshot logic — single source of truth.
from score_brief import _gather_data_snapshot, _judge  # noqa: E402

MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "opus": "claude-opus-4-7",
}

DIMENSIONS = ["factual_accuracy", "mandate_compliance", "specificity", "coach_voice", "restraint"]


async def _generate_to(model: str, out_path: Path) -> tuple[float, dict]:
    """Run the streaming generator, capture the final brief, write to out_path.

    Returns (elapsed_seconds, brief_dict).
    """
    t0 = time.perf_counter()
    final_brief: dict | None = None
    async for evt in briefing_mod.generate_streaming(model=model, save=False):
        if evt.get("type") == "done":
            final_brief = evt["brief"]
        elif evt.get("type") == "error":
            raise RuntimeError(f"brief stream errored: {evt.get('message')}")
    elapsed = time.perf_counter() - t0
    if final_brief is None:
        raise RuntimeError(f"{model} stream completed without a done event")
    out_path.write_text(json.dumps(final_brief, indent=2), encoding="utf-8")
    return elapsed, final_brief


async def _score(brief: dict, data: dict) -> dict:
    """Wrap _judge with timing."""
    t0 = time.perf_counter()
    result = await _judge(brief, data)
    result["_judge_seconds"] = time.perf_counter() - t0
    return result


async def _run(models: list[str]) -> None:
    data = _gather_data_snapshot()

    print("== Generating briefs ==")
    runs: list[dict] = []
    for label in models:
        model_id = MODEL_ALIASES.get(label, label)
        out_path = Path("/tmp") / f"brief_eval_{label}.json"
        print(f"  {label} ({model_id}) → {out_path}")
        elapsed, brief = await _generate_to(model_id, out_path)
        print(f"    {elapsed:.1f}s · {len(brief.get('takeaways', []))} takeaways")
        runs.append({"label": label, "model_id": model_id, "elapsed": elapsed, "brief": brief, "path": out_path})

    print()
    print("== Scoring ==")
    for run in runs:
        result = await _score(run["brief"], data)
        run["score"] = result
        print(f"  {run['label']}: total {result.get('total_pct', '?')}% (judge {result['_judge_seconds']:.1f}s)")

    # Side-by-side rubric table
    print()
    print("== Rubric (0-10 per dimension) ==")
    header = f"  {'dimension':<22}" + "".join(f"{r['label']:>12}" for r in runs)
    print(header)
    print("  " + "─" * (22 + 12 * len(runs)))
    for dim in DIMENSIONS:
        row = f"  {dim:<22}"
        for r in runs:
            score = r["score"].get("scores", {}).get(dim, "?")
            row += f"{score:>12}"
        print(row)
    print("  " + "─" * (22 + 12 * len(runs)))
    total_row = f"  {'TOTAL %':<22}"
    elapsed_row = f"  {'gen seconds':<22}"
    for r in runs:
        total_row += f"{r['score'].get('total_pct','?'):>12}"
        elapsed_row += f"{r['elapsed']:>12.1f}"
    print(total_row)
    print(elapsed_row)

    # Notes per brief
    print()
    print("== Notes ==")
    for r in runs:
        print(f"  [{r['label']}]")
        for note in r["score"].get("notes", []):
            print(f"    • {note}")
        print()

    # Verdict
    if len(runs) == 2:
        a, b = runs
        a_total = a["score"].get("total_pct", 0) or 0
        b_total = b["score"].get("total_pct", 0) or 0
        diff = b_total - a_total
        speedup = a["elapsed"] / b["elapsed"] if b["elapsed"] > 0 else 0
        print("== Verdict ==")
        print(f"  Quality delta ({b['label']} − {a['label']}): {diff:+.0f}%")
        print(f"  Speed ({b['label']} vs {a['label']}): {speedup:.2f}x")
        if diff >= -5 and speedup >= 1.5:
            print(f"  → RECOMMEND: switch DEFAULT_MODEL to {b['model_id']}")
            print(f"    ({b['label']} is {speedup:.1f}x faster with quality within 5pt of {a['label']})")
        elif diff < -5:
            print(f"  → KEEP: {a['model_id']}")
            print(f"    ({b['label']} regressed quality by {-diff:.0f}pt — speed not worth it)")
        else:
            print(f"  → KEEP: {a['model_id']}")
            print(f"    ({b['label']} not enough faster ({speedup:.1f}x) to justify a swap)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--models",
        default="sonnet,haiku",
        help="Comma-separated list of model labels (sonnet|haiku|opus or full id).",
    )
    args = p.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("No models specified.", file=sys.stderr)
        return 2
    asyncio.run(_run(models))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
