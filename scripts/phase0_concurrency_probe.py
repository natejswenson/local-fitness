#!/usr/bin/env python
"""Phase 0 concurrency probe for the brief fan-out design.

The map-reduce fan-out architecture is contingent on ONE empirical fact:
concurrent ``claude_agent_sdk.query()`` calls must actually run in parallel.
Each ``query()`` drives a Claude Code CLI subprocess on the Max-subscription
OAuth path; if those serialize (subprocess contention, single-session rate
limit), the fan-out is strictly slower than today's single call and the whole
design must fall back to "assemble-in-code + capped thinking on one call."

This script measures that, cheaply, with small identical-shape prompts (NOT
real briefs — we're measuring transport concurrency, not brief quality):

  * single-call baseline   T1
  * serial-of-3            T_serial3   (3 calls back-to-back)
  * concurrent-of-3        T_conc3     (asyncio.gather of 3)
  * concurrent-of-5        T_conc5     (asyncio.gather of 5)  -- confirm run

Kill criterion (from the design): speedup3 = T_serial3 / T_conc3 must be
>= 1.7. Below that, abandon fan-out. The 5-concurrent run confirms behavior
at production width (up to 5 composers + 1 planner).

Cost discipline: DRY-RUN by default (prints the plan + call count, no model
calls). Pass --run to actually execute. Hard cap on total calls.

Usage:
  uv run python scripts/phase0_concurrency_probe.py            # dry-run plan
  uv run python scripts/phase0_concurrency_probe.py --run      # execute probe
  uv run python scripts/phase0_concurrency_probe.py --run --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import asyncio
import time

# Small, fixed-shape work unit. ~120 words keeps each call in the few-second
# range so generation time dominates transport/startup noise, without burning
# real budget. Index varies the prompt so no two calls are cache-identical
# (identical prompts would only flatter concurrency).
_PROMPT = (
    "Write exactly one ~120-word paragraph of plain prose about the number {i} "
    "and one interesting mathematical property it has. No lists, no headings."
)

# Calls per stage. Hard cap is the sum; refuse to exceed it.
_SERIAL_N = 3
_CONC_N = 3
_CONFIRM_N = 5
_MAX_CALLS = 1 + _SERIAL_N + _CONC_N + _CONFIRM_N  # = 12
_KILL_SPEEDUP = 1.7


async def _one_call(model: str, i: int) -> dict:
    """Run a single query() to completion; return timing + any usage seen."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=1,
    )
    t0 = time.perf_counter()
    usage: dict | None = None
    async for message in query(prompt=_PROMPT.format(i=i), options=options):
        # ResultMessage (end of turn) carries usage on most SDK versions.
        u = getattr(message, "usage", None)
        if u is not None:
            usage = dict(u) if isinstance(u, dict) else getattr(u, "__dict__", {"raw": str(u)})
    return {"i": i, "secs": time.perf_counter() - t0, "usage": usage}


async def _serial(model: str, n: int, base: int) -> float:
    t0 = time.perf_counter()
    for k in range(n):
        await _one_call(model, base + k)
    return time.perf_counter() - t0


async def _concurrent(model: str, n: int, base: int) -> float:
    t0 = time.perf_counter()
    await asyncio.gather(*(_one_call(model, base + k) for k in range(n)))
    return time.perf_counter() - t0


async def _run(model: str) -> int:
    print(f"Phase 0 concurrency probe · model={model}\n")

    print("• warmup / single-call baseline (1 call)…")
    base = await _one_call(model, 0)
    t1 = base["secs"]
    print(f"  T1 = {t1:.1f}s  usage={base['usage']}")

    print(f"\n• serial-of-{_SERIAL_N} ({_SERIAL_N} calls back-to-back)…")
    t_serial3 = await _serial(model, _SERIAL_N, base=10)
    print(f"  T_serial3 = {t_serial3:.1f}s  (avg/call {t_serial3/_SERIAL_N:.1f}s)")

    print(f"\n• concurrent-of-{_CONC_N} (asyncio.gather)…")
    t_conc3 = await _concurrent(model, _CONC_N, base=20)
    print(f"  T_conc3 = {t_conc3:.1f}s")

    speedup3 = t_serial3 / t_conc3 if t_conc3 > 0 else 0.0
    print(f"\n  >>> speedup(3) = T_serial3 / T_conc3 = {speedup3:.2f}x "
          f"(kill criterion: >= {_KILL_SPEEDUP}x)")

    print(f"\n• concurrent-of-{_CONFIRM_N} (production-width confirm)…")
    t_conc5 = await _concurrent(model, _CONFIRM_N, base=30)
    est_serial5 = t1 * _CONFIRM_N  # conservative estimate from single-call T1
    speedup5 = est_serial5 / t_conc5 if t_conc5 > 0 else 0.0
    print(f"  T_conc5 = {t_conc5:.1f}s  (est serial5 ~= {est_serial5:.1f}s → "
          f"~{speedup5:.2f}x)")

    print("\n" + "=" * 60)
    if speedup3 >= _KILL_SPEEDUP:
        print(f"VERDICT: PROCEED with fan-out. "
              f"speedup(3)={speedup3:.2f}x >= {_KILL_SPEEDUP}x.")
        print(f"         5-wide confirm ~{speedup5:.2f}x.")
        return 0
    print(f"VERDICT: ABANDON fan-out. speedup(3)={speedup3:.2f}x < {_KILL_SPEEDUP}x.")
    print("         Fall back to: assemble-in-code + capped thinking on ONE call.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually execute (else dry-run)")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    args = ap.parse_args()

    if not args.run:
        print("DRY RUN — no model calls.\n")
        print(f"Plan: 1 baseline + {_SERIAL_N} serial + {_CONC_N} concurrent "
              f"+ {_CONFIRM_N} confirm = {_MAX_CALLS} small calls "
              f"(~120-word prose each), model claude-sonnet-4-6.")
        print(f"Kill criterion: speedup(3) >= {_KILL_SPEEDUP}x → proceed with fan-out.")
        print("Runs on the Max-subscription OAuth session (no metered $).")
        print("\nRe-run with --run to execute.")
        return 0

    return asyncio.run(_run(args.model))


if __name__ == "__main__":
    raise SystemExit(main())
