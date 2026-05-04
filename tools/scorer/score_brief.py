"""Score a generated brief against a measurable rubric.

The decision this scorer informs: should we flip ``DEFAULT_MODEL`` from
Sonnet 4.6 to Haiku 4.5 for the daily brief? Haiku emits tokens 2-3x
faster, but only matters if the briefs are still good. "Eyeballing 3
briefs" was explicitly disallowed by the global preferences — this is
the automated alternative.

Rubric (each dimension scored 0-10 by an LLM judge — Sonnet 4.6 — given
both the brief and the underlying SQLite data):

  factual_accuracy    every cited number matches the data, no fabrication
  mandate_compliance  the workout, steps, and RHR-anomaly takeaways are present
  specificity         takeaways cite specific numbers + time windows
  coach_voice         tone matches the system prompt's rules (no commands,
                      translates technical metrics on first use, plain English)
  restraint           tight prose, no padding, no generic fitness advice

Total = sum of dimensions / 50, expressed as percentage.

Usage::

    uv run python tools/scorer/score_brief.py briefings/2026-04-27.json
    uv run python tools/scorer/score_brief.py briefings/2026-04-27-haiku.json --label haiku
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from local_fitness import db  # noqa: E402


JUDGE_SYSTEM = """You are an evaluator scoring the quality of a daily fitness brief produced for a runner. The brief is structured JSON with 3-5 takeaways. You'll be given (a) the brief, and (b) a snapshot of the underlying database the brief was generated from. Your job is to score the brief on a precise rubric and return strict JSON.

Be a tough grader. Hedge less, be specific. If a brief fabricates a number, that's a fail on factual_accuracy. If takeaways are generic ("get more sleep"), that's a fail on specificity."""


JUDGE_PROMPT_TEMPLATE = """# Brief to score

```json
{brief_json}
```

# Underlying data snapshot (last 14 days + baseline)

```json
{data_json}
```

# Rubric (each 0-10)

1. **factual_accuracy** — Every number cited in the brief should appear in or be a correct derivation from the data snapshot. Penalize fabricated numbers, wrong directions of trend, mismatched dates. 10 = every number checks out. 0 = multiple fabrications.
2. **mandate_compliance** — These three takeaways should appear in EVERY brief: (a) today's recommended workout, (b) daily steps progress vs the user's goal (10000), (c) an RHR anomaly check or recent-RHR observation. 10 = all three present and on-topic. Subtract ~3 per missing/off-topic mandate.
3. **specificity** — Each takeaway must cite concrete numbers + time windows. Penalize anything like "trending in the right direction" without a specific number. 10 = every takeaway is grounded. 0 = mostly vibes.
4. **coach_voice** — Reads like a personal coach texting before a run. NOT clinical, NOT a chart, NOT a command ("you must"). Translates technical metrics (CTL → "fitness", TSB → "freshness") on first use. Plain English over standard deviations.
5. **restraint** — Tight prose. Each takeaway pulls weight. No padding, no "remember to listen to your body" filler, no generic advice.

Return STRICT JSON, no preamble, no fences:

```
{{
  "scores": {{
    "factual_accuracy": <0-10>,
    "mandate_compliance": <0-10>,
    "specificity": <0-10>,
    "coach_voice": <0-10>,
    "restraint": <0-10>
  }},
  "total_pct": <0-100, sum of scores * 2>,
  "notes": [
    "<one sentence per concrete observation, positive or negative>",
    ...
  ]
}}
```"""


def _gather_data_snapshot() -> dict:
    """Pull the data the brief is supposed to be grounded in.

    14 days of daily metrics + the most recent baseline + the 7 most recent
    workouts. Same shape the agent's MCP tools would have surfaced.
    """
    today = date.today()
    fortnight_ago = (today - timedelta(days=14)).isoformat()
    with db.connect() as conn:
        recent = conn.execute(
            "SELECT date, sleep_seconds, sleep_score, rhr, avg_stress, "
            "body_battery_min, body_battery_max, steps "
            "FROM daily_metrics WHERE date >= ? ORDER BY date DESC",
            (fortnight_ago,),
        ).fetchall()
        baseline = conn.execute(
            "SELECT * FROM baselines ORDER BY date DESC LIMIT 1"
        ).fetchone()
        workouts = conn.execute(
            "SELECT date, activity_type, distance_meters, duration_seconds, "
            "avg_hr, training_load, aerobic_te FROM activities "
            "ORDER BY start_time DESC LIMIT 7"
        ).fetchall()
    return {
        "today": today.isoformat(),
        "recent_14d": [dict(r) for r in recent],
        "baseline": dict(baseline) if baseline else None,
        "recent_workouts": [dict(w) for w in workouts],
    }


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"No JSON found in judge response:\n{text[:500]}")


async def _judge(brief: dict, data: dict) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=JUDGE_SYSTEM,
        model="claude-sonnet-4-6",
        permission_mode="bypassPermissions",
        allowed_tools=[],
        max_turns=1,
    )
    chunks: list[str] = []
    async for message in query(
        prompt=JUDGE_PROMPT_TEMPLATE.format(
            brief_json=json.dumps(brief, indent=2),
            data_json=json.dumps(data, indent=2, default=str),
        ),
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return _extract_json("\n".join(chunks))


def main() -> int:
    p = argparse.ArgumentParser(description="Score a fitness brief.")
    p.add_argument("brief_path", help="Path to a brief JSON file.")
    p.add_argument("--label", default="", help="Optional label printed alongside scores.")
    args = p.parse_args()

    bp = Path(args.brief_path).expanduser().resolve()
    if not bp.exists():
        print(f"Brief not found: {bp}", file=sys.stderr)
        return 2

    brief = json.loads(bp.read_text(encoding="utf-8"))
    data = _gather_data_snapshot()

    print(f"Scoring {bp.name}{' [' + args.label + ']' if args.label else ''}…")
    result = asyncio.run(_judge(brief, data))

    scores = result.get("scores", {})
    total = result.get("total_pct", 0)
    notes = result.get("notes", [])

    print()
    print(f"  factual_accuracy   : {scores.get('factual_accuracy', '?')}/10")
    print(f"  mandate_compliance : {scores.get('mandate_compliance', '?')}/10")
    print(f"  specificity        : {scores.get('specificity', '?')}/10")
    print(f"  coach_voice        : {scores.get('coach_voice', '?')}/10")
    print(f"  restraint          : {scores.get('restraint', '?')}/10")
    print("  ───────────────────────────")
    print(f"  TOTAL              : {total}%")
    print()
    if notes:
        print("Notes:")
        for n in notes:
            print(f"  • {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
