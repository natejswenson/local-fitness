"""Brand theme for every generated PDF/PNG artifact (visuals.py).

The repo default is the PRESS brand — the warm-paper editorial system
Nate's site, ghostwriter cards, and devlog covers share (canonical
sources: ghostwriter `diagram.css` `.card.press` + the devlog image
style guide, 2026-07-18). Because this is a public repo, the theme is
**local-overridable**: point ``LOCAL_FITNESS_BRAND_FILE`` at a JSON
file (``data/brand.json`` is the natural gitignored home) and its keys
deep-merge over the default — swap one color or replace the whole
identity without touching tracked code. A missing or broken brand file
must never break a render: any load error logs a warning and falls back
to the default theme.

PRESS rules the default encodes (see the style guide's "never do"):
flat paper, ink rules for structure, no rounded corners / shadows /
gradients, and ONE signature accent used sparingly — tones and verdicts
are carried typographically (ink / dim / italic), with the accent
reserved for critical/missed items.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

_BRAND_FILE_ENV = "LOCAL_FITNESS_BRAND_FILE"

DEFAULT_THEME: dict = {
    "name": "press",
    "colors": {
        # Warm cream paper — flat, never gradiented or textured.
        "paper": "#F5F0E6",
        # Near-black ink: text, headlines, and every structural rule.
        "ink": "#181510",
        # Muted secondary text (the serif "commentary" voice's color).
        "dim": "#6E675C",
        # THE one loud color. Critical/missed only; zero on a good day.
        "accent": "#E8501F",
        # Rules are ink — named separately so an override can soften them
        # without touching text color.
        "rule": "#181510",
    },
    "fonts": {
        # Display/structure voice: system sans, set heavy (800-900) with
        # tight tracking by the CSS — nothing is loaded from disk.
        "display_stack": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif",
        # Commentary voice: serif italics for standfirsts/captions.
        "serif_stack": "'New York', ui-serif, Georgia, 'Times New Roman', serif",
        # Data voice: system mono stack by default; visually ~Plex Mono at
        # PDF data sizes. Set ``mono_file`` to a real TTF (e.g. IBM Plex
        # Mono) to load the authentic face via @font-face locally.
        "mono_stack": "ui-monospace, 'SF Mono', Menlo, monospace",
        "mono_file": None,
    },
    "identity": {
        # Typographic stamp (rotated square, accent border + initials).
        "stamp": "NS",
        # Masthead eyebrow, tracked caps: "{brand_line} · MORNING BRIEF · date".
        "brand_line": "LOCAL FITNESS",
        # Right-aligned dim byline in the masthead.
        "byline": "linkedin.com/in/natejswenson",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``. Non-dict
    override values replace; unknown keys are kept (forward-compatible —
    an override may carry keys a newer default hasn't named yet)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_theme() -> dict:
    """The active brand theme: ``DEFAULT_THEME`` deep-merged with the JSON
    file named by ``LOCAL_FITNESS_BRAND_FILE`` (if set and readable).

    Called per render, not cached — editing the brand file takes effect on
    the next PDF without a process restart. ``fonts.mono_file`` gets ``~``
    expansion so a home-relative path works from launchd/compose contexts.
    """
    theme = copy.deepcopy(DEFAULT_THEME)
    brand_file = os.environ.get(_BRAND_FILE_ENV)
    if brand_file:
        try:
            override = json.loads(Path(brand_file).expanduser().read_text(encoding="utf-8"))
            if isinstance(override, dict):
                theme = _deep_merge(theme, override)
            else:
                _LOG.warning(
                    "brand file %s is not a JSON object — using default theme", brand_file)
        except (OSError, ValueError):
            _LOG.warning(
                "could not load brand file %s — using default theme",
                brand_file, exc_info=True)
    mono_file = theme.get("fonts", {}).get("mono_file")
    if mono_file:
        theme["fonts"]["mono_file"] = str(Path(mono_file).expanduser())
    return theme
