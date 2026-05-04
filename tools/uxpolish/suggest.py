"""Generate 3 incremental UI variants for a single React component.

Reads ``web/src/components/<Name>.tsx``, asks Claude (via the Agent SDK so
the host's Claude Code OAuth is reused — no API key needed) for three
distinct className/JSX-only polish variants, and writes them as
``<Name>.v1.tsx`` / ``.v2.tsx`` / ``.v3.tsx`` next to the original.

Usage::

    uv run python tools/uxpolish/suggest.py StatCard
    uv run python tools/uxpolish/suggest.py TakeawayCard
    uv run python tools/uxpolish/suggest.py ChatPanel

Then open ``https://fitness.home.local/__uxpolish?component=<Name>`` in a
browser (or on your phone) to flip between the original and the three
variants. When you've picked a winner, run::

    uv run python tools/uxpolish/apply.py <Name> --pick <1|2|3>

which replaces the original file with the chosen variant and cleans up
the rest. Then rebuild the container so the URL serves the new version.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

REPO = Path(__file__).resolve().parents[2]
COMPONENTS_DIR = REPO / "web" / "src" / "components"
TOKENS_PATH = REPO / "web" / "src" / "index.css"

ALLOWED_COMPONENTS = {"StatCard", "TakeawayCard", "ChatPanel", "Sidebar", "SyncIndicator", "Card"}


SYSTEM_PROMPT = """You are a senior product designer obsessed with clean, simple, polished interfaces. The product is a personal fitness coaching app — runners' data, takeaway cards, daily briefings — used on iPhone and desktop. The user's stated taste: "amazing simple and clean UI." Restraint over decoration. Hierarchy, whitespace, and typographic precision over color and ornament.

The codebase uses Tailwind v4 with OKLch design tokens. Color tokens (CSS custom properties): --color-bg, --color-surface, --color-surface-2, --color-border, --color-text, --color-muted, --color-faint, --color-accent (single brand green), --color-accent-dim, --color-good, --color-warn, --color-bad. Tailwind utilities: bg-bg, bg-surface, bg-surface-2, text-text, text-muted, text-faint, text-accent, border-border, etc. Typography: Inter var (`font-sans`) and JetBrains Mono (`font-mono`). Numerics use `tabular-nums`. Card radius: rounded-xl (12px).

Hard rules for your edits:
- ONLY change className strings, JSX nesting/structure, and whitespace.
- DO NOT add new imports.
- DO NOT add or remove useState/useEffect/useRef hooks.
- DO NOT add data-fetching, event handlers beyond what's already there, or new component logic.
- DO NOT change prop interfaces or function signatures.
- The output must be a complete, drop-in valid TSX file that compiles against the existing types.
"""


USER_PROMPT_TEMPLATE = """Component file: `web/src/components/{name}.tsx`

```tsx
{source}
```

Propose THREE distinct incremental polish variants. Each should be a single coherent design idea — not a kitchen sink. Examples of good variant themes (don't all have to be these):
- Tighter spacing and stronger hierarchy
- More restraint — strip ornament, lean into whitespace
- Subtler dividers and softer surfaces
- Sharper typographic contrast
- Tabular precision (numbers and labels aligned exactly)

Output STRICT JSON (no preamble, no code fences, just the JSON object):

```
{{
  "variants": [
    {{"name": "<3-5 word theme>", "rationale": "<one sentence on what changed and why>", "file": "<full TSX file content>"}},
    {{"name": "...", "rationale": "...", "file": "..."}},
    {{"name": "...", "rationale": "...", "file": "..."}}
  ]
}}
```

Each `file` field MUST be the complete TSX file ready to drop in. Preserve all imports, all type definitions, all prop names, all function names. Only change className strings, JSX structure, and whitespace."""


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the model's response. Handles code fences
    and leading/trailing prose."""
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
    raise ValueError(f"No JSON found in response (first 500 chars):\n{text[:500]}")


async def _propose(name: str, source: str) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model="claude-sonnet-4-6",
        permission_mode="bypassPermissions",
        # No MCP tools — pure text generation.
        allowed_tools=[],
        max_turns=1,
    )
    chunks: list[str] = []
    async for message in query(
        prompt=USER_PROMPT_TEMPLATE.format(name=name, source=source),
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    raw = "\n".join(chunks).strip()
    return _extract_json(raw)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate 3 incremental UI variants for a React component."
    )
    p.add_argument("component", help=f"Component name (one of {sorted(ALLOWED_COMPONENTS)})")
    args = p.parse_args()

    if args.component not in ALLOWED_COMPONENTS:
        print(f"Unknown component '{args.component}'. Allowed: {sorted(ALLOWED_COMPONENTS)}", file=sys.stderr)
        return 2

    src_path = COMPONENTS_DIR / f"{args.component}.tsx"
    if not src_path.exists():
        print(f"Source not found: {src_path}", file=sys.stderr)
        return 2

    source = src_path.read_text(encoding="utf-8")
    print(f"Proposing 3 variants for {args.component}.tsx ({len(source)} chars)…")

    payload = asyncio.run(_propose(args.component, source))
    variants = payload.get("variants", [])
    if len(variants) != 3:
        print(f"Expected 3 variants, got {len(variants)}", file=sys.stderr)
        return 1

    for i, v in enumerate(variants, start=1):
        out_path = COMPONENTS_DIR / f"{args.component}.v{i}.tsx"
        out_path.write_text(v["file"], encoding="utf-8")
        print(f"  v{i}  {v.get('name','(unnamed)')}: {v.get('rationale','')}")
        print(f"       → {out_path.relative_to(REPO)}")

    print()
    print("Open in browser (after rebuild):")
    print(f"  https://fitness.home.local/__uxpolish?component={args.component}")
    print()
    print("When you've picked a winner:")
    print(f"  uv run python tools/uxpolish/apply.py {args.component} --pick <1|2|3>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
