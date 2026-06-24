#!/usr/bin/env python3
"""Score EVERY coach tone profile against its expected outcomes.

`score_prompt.py` scores only the default (adaptive) rendering. This scores all
four profiles — supportive / neutral / hardass / adaptive — at the PROMPT level,
deterministically (no LLM, no network), so the selectable tone behavior is
falsifiable in CI: a profile whose prose drifts from its intent, or whose
deterministic harsh-block gating breaks, fails loudly here.

Two kinds of checks per profile:
  * Universal (every profile must keep): the four Tone words, the CTL/ATL/TSB
    jargon translation, and the schema-FIXED language — so every profile still
    produces schema-valid, grounded briefs.
  * Expected outcome (per profile): adaptive/hardass include the harsh-tone
    imperative block; supportive/neutral omit it; adaptive contains "roast";
    hardass carries accountability language; supportive carries warmth markers.

Usage:
    python3 scripts/score_profiles.py [--verbose]
Exits non-zero if any required check fails.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_TONE_WORDS = ("positive", "caution", "critical", "neutral")
_JARGON = ("ctl", "atl", "tsb", "fitness", "fatigue", "freshness")
_SCHEMA_FIXED = ("non-negotiable", "exactly one key", "takeaways")
_HARSH_MARKER = "be sharp. be harsh"  # the goal-based harsh-tone imperative block


def build_checks() -> list[tuple[str, bool]]:
    from local_fitness.agent import coach, prompts

    checks: list[tuple[str, bool]] = []
    for name in sorted(coach.PROFILE_NAMES):
        p = coach.load_profile(name)
        sp = prompts.system_prompt("TestRunner", p).lower()
        bp = prompts.briefing_prompt("TestRunner", 10000, "", p).lower()

        # universal — every profile keeps these
        checks.append((f"[{name}] persona is non-empty", bool(p.persona.strip())))
        checks.append((f"[{name}] keeps all four Tone words", all(t in bp for t in _TONE_WORDS)))
        checks.append((f"[{name}] keeps CTL/ATL/TSB jargon translation", all(j in sp for j in _JARGON)))
        checks.append((f"[{name}] keeps schema-FIXED language", all(s in bp for s in _SCHEMA_FIXED)))
        checks.append((f"[{name}] system prompt names the profile", name in sp))

        # expected outcome — deterministic harsh-block gating (the falsifiable core)
        harsh_present = _HARSH_MARKER in bp
        if name in ("adaptive", "hardass"):
            checks.append((f"[{name}] INCLUDES the harsh-tone steps block", harsh_present))
        else:  # supportive, neutral
            checks.append((f"[{name}] OMITS the harsh-tone steps block", not harsh_present))

    # per-profile voice markers
    sp_adaptive = prompts.system_prompt("TestRunner", coach.load_profile("adaptive")).lower()
    checks.append(("[adaptive] persona contains 'roast' (accountability)", "roast" in sp_adaptive))

    sp_hardass = prompts.system_prompt("TestRunner", coach.load_profile("hardass")).lower()
    checks.append((
        "[hardass] persona carries accountability language",
        any(m in sp_hardass for m in ("no excuse", "this is on you", "stop coasting", "do better")),
    ))

    sp_supportive = prompts.system_prompt("TestRunner", coach.load_profile("supportive")).lower()
    checks.append((
        "[supportive] persona carries warmth markers",
        any(m in sp_supportive for m in ("encourag", "you've got", "nice work", "bounce-back", "believer")),
    ))

    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score all coach profiles vs expected outcomes")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    checks = build_checks()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    for desc, ok in checks:
        if args.verbose or not ok:
            print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print(f"\nProfiles score: {passed}/{total} ({round(100 * passed / total)}%) — coach profiles")
    if passed < total:
        print("FAILED: a profile drifted from its expected outcome.")
        return 1
    print("PASSED: every profile matches its expected outcome.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
