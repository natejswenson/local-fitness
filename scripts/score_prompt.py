#!/usr/bin/env python3
"""Score the agent prompt against explicit pass/fail checks.

A coaching agent is code. Its prompt (``agent/prompts.py``) is the part you
can't compile — so you score it instead. This runs grounded pass/fail checks
(not style nits) covering the things the prompt MUST get right, prints a
score, and exits non-zero if any required check fails so CI can gate on it.

The highest-value check is the schema cross-check: the briefing prompt hands
Claude a JSON example, and ``agent/schemas.py`` is the Pydantic contract the
output is validated against. If the two drift — a metric named in the prompt
that the schema rejects, or a tone the schema doesn't allow — briefs break
silently (the frontend drops unknown keys). This scorer fails loudly the day
that happens.

Usage:
    python3 scripts/score_prompt.py            # score the live prompt
    python3 scripts/score_prompt.py --verbose  # also print each check's detail

Imports the real ``local_fitness`` package — no network, no Claude call.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# PEP 440-ish: a release segment plus optional pre/post/dev suffixes. Enough to
# reject an empty or obviously-malformed version without pulling in packaging.
_VERSION_RE = re.compile(r"^\d+(\.\d+)*([abc]|rc|\.post|\.dev)?\d*$")


def _pyproject_version() -> str:
    """Pull the project version out of pyproject.toml (stdlib tomllib)."""
    import tomllib

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", ""))


def build_checks() -> list[tuple[str, bool]]:
    """Return a list of (description, passed) pass/fail checks.

    Imported lazily inside the function so a failed import surfaces as a
    crash with a traceback rather than a silent half-run at module load.
    """
    from local_fitness.agent import prompts, schemas

    system = prompts.system_prompt("TestRunner")
    briefing = prompts.briefing_prompt()
    sys_low = system.lower()
    brief_low = briefing.lower()

    # --- schema cross-check: every metric/tone the prompt advertises must be
    # a member of the Pydantic enums the output is validated against. ---
    allowed_metrics = set(schemas.MetricName.__args__)
    allowed_tones = set(schemas.Tone.__args__)
    # The briefing prompt lists metrics in a `<one of: a | b | c>` block.
    metric_block = re.search(r"one of:\s*([a-z0-9_ |]+)", briefing)
    prompt_metrics = (
        {m.strip() for m in metric_block.group(1).split("|") if m.strip()}
        if metric_block
        else set()
    )
    metrics_consistent = bool(prompt_metrics) and prompt_metrics <= allowed_metrics
    # The briefing prompt's JSON example pins tones in a `"tone": "a | b | c"`
    # block. Anchor to that enumerated block (same shape as the metric block
    # above) rather than a loose substring scan — the tone words also appear in
    # prose, so a bare `in brief_low` check can't actually catch a drift.
    tone_block = re.search(r'"tone":\s*"([a-z |]+)"', briefing)
    prompt_tones = (
        {t.strip() for t in tone_block.group(1).split("|") if t.strip()}
        if tone_block
        else set()
    )
    tones_consistent = prompt_tones == allowed_tones

    # --- user-notes injection is wired (the durable-preferences lever). ---
    notes_injected = _probe_notes_injection(prompts)

    return [
        ("system_prompt() returns non-empty text", bool(system.strip())),
        (
            "states the never-fabricate-numbers rule",
            "never fabricate numbers" in sys_low,
        ),
        (
            "translates CTL/ATL/TSB jargon to fitness/fatigue/freshness",
            all(j in system for j in ("CTL", "ATL", "TSB"))
            and all(w in sys_low for w in ("fitness", "fatigue", "freshness")),
        ),
        (
            "declares the roast-when-slipping tone",
            "roast" in sys_low,
        ),
        (
            "references the MCP fitness tools",
            "mcp__fitness__" in system,
        ),
        (
            "injects durable user notes into the system prompt",
            notes_injected,
        ),
        (
            "briefing prompt marks the schema FIXED / NON-NEGOTIABLE",
            "non-negotiable" in brief_low and "fixed" in brief_low,
        ),
        (
            "briefing prompt requires exactly one top-level key (takeaways)",
            "exactly one key" in brief_low and "takeaways" in brief_low,
        ),
        (
            "briefing prompt's metrics are all valid schema MetricName values",
            metrics_consistent,
        ),
        (
            "briefing prompt's tones match schema Tone values exactly",
            tones_consistent,
        ),
        (
            "pyproject.toml has a PEP 440-valid version",
            bool(_VERSION_RE.match(_pyproject_version())),
        ),
    ]


def _probe_notes_injection(prompts) -> bool:
    """Confirm system_prompt() actually folds in saved notes by injecting a
    sentinel through the notes module the prompt reads from."""
    notes_mod = prompts.user_notes_mod
    original = notes_mod.render_for_prompt
    sentinel = "INJECTED-NOTE-PROBE-XYZZY"
    try:
        notes_mod.render_for_prompt = lambda *a, **k: sentinel
        return sentinel in prompts.system_prompt("TestRunner")
    finally:
        notes_mod.render_for_prompt = original


def score(verbose: bool = False) -> int:
    """Run the checks, print results, and return a process exit code."""
    checks = build_checks()
    passed = 0
    for desc, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
        if ok:
            passed += 1

    total = len(checks)
    pct = round(100 * passed / total)
    print(f"\nScore: {passed}/{total} ({pct}%) — agent/prompts.py")
    if passed < total:
        print("FAILED: one or more required checks did not pass.", file=sys.stderr)
        return 1
    print("PASSED: all required checks passed.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true", help="(reserved) verbose output")
    args = ap.parse_args()
    sys.exit(score(verbose=args.verbose))


if __name__ == "__main__":
    main()
