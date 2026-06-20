#!/usr/bin/env python
"""Phase 0 quality A/B: does effort=low regress brief quality vs the current
(effort≈high) default?

Speed is settled (effort=low ≈ 82s vs ~230s). Quality is sacred and must not be
eyeballed, so this generates paired briefs at both efforts (same day, same data;
they differ only by reasoning effort + sampling) and writes them to a gitignored
temp dir for an LLM-judge to score blind. Generation only — judging is a
separate agent step that reads the dumped files.

Usage:
  uv run python scripts/phase0_quality_ab.py --run --k 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

_OUT = Path(__file__).resolve().parent.parent / "briefings" / "_ab"


async def _gen_one(effort: str, idx: int) -> Path:
    # Set effort BEFORE importing/using briefing so _brief_effort() reads it.
    os.environ["LOCAL_FITNESS_BRIEF_EFFORT"] = effort
    from local_fitness.agent import briefing

    brief = None
    async for evt in briefing.generate_streaming(save=False):
        if evt["type"] == "done":
            brief = evt["brief"]
        elif evt["type"] == "error":
            raise RuntimeError(evt["message"])
    if brief is None:
        raise RuntimeError("no done event")
    # Blind filename (arm not in the name the judge sees); keep a side manifest.
    path = _OUT / f"brief_{effort}_{idx}.json"
    path.write_text(json.dumps(brief, indent=2, default=str), encoding="utf-8")
    return path


async def _run(k: int) -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    made = []
    # Interleave arms so any data drift across the run hits both equally.
    for i in range(k):
        for effort in ("high", "low"):
            p = await _gen_one(effort, i)
            print(f"  wrote {p.name}")
            made.append(p.name)
    print(f"\nGenerated {len(made)} briefs in {_OUT}")
    print("Arms: 'high' = current default-equivalent, 'low' = candidate.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--k", type=int, default=3, help="paired samples per arm")
    args = ap.parse_args()
    if not args.run:
        print(f"DRY RUN. Would generate {args.k} high + {args.k} low briefs "
              f"(~{args.k}×230s + {args.k}×82s) into {_OUT}.")
        return 0
    return asyncio.run(_run(args.k))


if __name__ == "__main__":
    raise SystemExit(main())
