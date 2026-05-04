"""Apply a chosen variant — replaces the original component with the picked
variant and removes the other ``.vN.tsx`` siblings.

Usage::

    uv run python tools/uxpolish/apply.py StatCard --pick 2
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPONENTS_DIR = REPO / "web" / "src" / "components"


def main() -> int:
    p = argparse.ArgumentParser(description="Apply a chosen UX-polish variant.")
    p.add_argument("component", help="Component name (e.g. StatCard)")
    p.add_argument("--pick", type=int, choices=[1, 2, 3], required=True)
    args = p.parse_args()

    src = COMPONENTS_DIR / f"{args.component}.tsx"
    pick = COMPONENTS_DIR / f"{args.component}.v{args.pick}.tsx"
    if not src.exists():
        print(f"Original not found: {src}", file=sys.stderr)
        return 2
    if not pick.exists():
        print(f"Variant not found: {pick}", file=sys.stderr)
        return 2

    # Replace original with the picked variant.
    shutil.copyfile(pick, src)
    print(f"Replaced {src.relative_to(REPO)} with v{args.pick}")

    # Clean up all .vN.tsx siblings.
    removed = 0
    for v in COMPONENTS_DIR.glob(f"{args.component}.v*.tsx"):
        v.unlink()
        removed += 1
    print(f"Removed {removed} variant file(s)")

    print()
    print("Rebuild the container so the new version is live at fitness.home.local:")
    print("  cd ~/localrepo/traefik && docker compose up -d --build local-fitness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
