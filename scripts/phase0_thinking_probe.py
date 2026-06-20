#!/usr/bin/env python
"""Phase 0 diagnostic: does ANY thinking knob actually propagate on this path?

A full brief at thinking budget_tokens=3072 produced ~14.4k output tokens —
identical to the uncapped run — so ClaudeAgentOptions.thinking appears INERT on
the Claude Code CLI / Max-OAuth transport. Before redesigning around that, this
isolates which knob (if any) the path honors, cheaply: one reasoning-heavy
prompt run under several configs, comparing output_tokens (which includes
thinking tokens). A large spread ⇒ the knob works; flat ⇒ thinking is
CLI-controlled and not cappable from here.

Usage:
  uv run python scripts/phase0_thinking_probe.py --run
"""
from __future__ import annotations

import argparse
import asyncio
import time

# A prompt that genuinely invites multi-step reasoning, so adaptive thinking
# spends real tokens and a cap/disable produces a visible delta.
_PROMPT = (
    "Three people check into a hotel room costing $30, paying $10 each. The "
    "clerk realizes the room is $25 and sends $5 back via the bellhop, who "
    "keeps $2 and returns $1 to each guest. Now each paid $9 (=$27) plus the "
    "bellhop's $2 is $29 — where is the missing dollar? Reason carefully step "
    "by step, then give the resolution in one sentence."
)

_CONFIGS = [
    ("default(None)", None),
    ("disabled", {"type": "disabled"}),
    ("enabled-1024", {"type": "enabled", "budget_tokens": 1024}),
    ("enabled-12000", {"type": "enabled", "budget_tokens": 12000}),
]
_EFFORTS = ["low", "high"]


async def _call(thinking, effort) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, query

    kw = dict(model="claude-sonnet-4-6", permission_mode="bypassPermissions", max_turns=1)
    if thinking is not None:
        kw["thinking"] = thinking
    if effort is not None:
        kw["effort"] = effort
    opts = ClaudeAgentOptions(**kw)
    t0 = time.perf_counter()
    out = None
    async for m in query(prompt=_PROMPT, options=opts):
        u = getattr(m, "usage", None)
        if u is not None:
            out = dict(u) if isinstance(u, dict) else getattr(u, "__dict__", {})
    secs = time.perf_counter() - t0
    return {"secs": secs, "output_tokens": (out or {}).get("output_tokens")}


async def _run() -> int:
    print("Thinking-knob propagation probe (one reasoning prompt per config)\n")
    print(f"{'config':<22}{'output_tokens':>14}{'secs':>9}")
    print("-" * 45)
    for name, cfg in _CONFIGS:
        r = await _call(cfg, None)
        print(f"{name:<22}{str(r['output_tokens']):>14}{r['secs']:>9.1f}")
    for eff in _EFFORTS:
        r = await _call(None, eff)
        print(f"{'effort='+eff:<22}{str(r['output_tokens']):>14}{r['secs']:>9.1f}")
    print("\nIf output_tokens varies widely → a knob works (use it).")
    print("If output_tokens is flat across all → thinking is CLI-controlled here.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        print("DRY RUN. 6 small reasoning calls (4 thinking configs + 2 efforts).")
        print("Re-run with --run.")
        return 0
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
