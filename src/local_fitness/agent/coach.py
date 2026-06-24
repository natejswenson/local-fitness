"""Coach tone profiles — the selectable voice the brief/chat speaks in.

A profile is a `coach_profiles/<name>.md` file: YAML-ish frontmatter (the numeric
dials) + a fleshed prose body (the tone/voice). The selected profile is resolved
``settings DB > env var > default`` and threaded into the prompts.

Two tiers of dials, by how falsifiable they are:
  * ``harshness`` / ``warmth`` / ``push`` (0-10) — prose CALIBRATION hints
    interpolated into the prompt; directional, LLM-judged, not finely controllable.
  * ``roast_threshold`` / ``praise_threshold`` (fractions of goal) — these
    DETERMINISTICALLY gate which harsh-tone imperative blocks the briefing prompt
    assembles for goal-based mandates (steps, plan adherence). That is the
    testable behavior; see ``prompts.briefing_prompt``.

Import-safe by construction: if a profile file is missing or malformed, loading
falls back to an in-code constant, so importing this module (and ``prompts``,
which builds module constants at import) never raises.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .. import config, db

_PROFILE_DIR = Path(__file__).resolve().parent / "coach_profiles"

#: valid profile names (also the shipped files). Unknown → adaptive.
PROFILE_NAMES = frozenset({"adaptive", "supportive", "neutral", "hardass"})
DEFAULT_PROFILE = "adaptive"

# Dial bounds (clamp on resolve; out-of-range → the profile's own value).
_DIAL_MIN, _DIAL_MAX = 0, 10
_THRESH_MIN, _THRESH_MAX = 0.0, 1.20

#: harshness at/above which the goal-based mandates assemble the harsh-tone
#: imperative block ("Be sharp. Be harsh. Override the usual voice"). adaptive(6)
#: + hardass(9) include it; neutral(5) + supportive(1) omit it and let their
#: persona govern how a missed goal reads. This is the deterministic, testable gate.
HARSH_BLOCK_MIN = 6


@dataclass(frozen=True)
class CoachProfile:
    """Resolved tone profile threaded into the prompts."""
    name: str
    harshness: int
    warmth: int
    push: int
    roast_threshold: float
    praise_threshold: float
    persona: str  # the .md body — the tone/voice bullets

    @property
    def includes_harsh_block(self) -> bool:
        """Whether goal-based mandates assemble the harsh-tone imperative block.
        Gated on harshness so neutral (factual, harshness 5) is excluded even
        though its roast_threshold matches adaptive's."""
        return self.harshness >= HARSH_BLOCK_MIN

    @property
    def dials_line(self) -> str:
        return (
            f"Coaching dials: harshness {self.harshness}/10 · warmth "
            f"{self.warmth}/10 · push {self.push}/10 · harden the tone when below "
            f"{self.roast_threshold:.2f} of goal · celebrate above "
            f"{self.praise_threshold:.2f} of goal (thresholds apply to goal-based "
            f"signals like steps and plan adherence)."
        )


# In-code fallback so a missing/broken adaptive.md never bricks import. Mirrors
# the historical persona (must contain "roast" so the prompt scorer survives even
# in the degraded path).
_FALLBACK_ADAPTIVE = CoachProfile(
    name="adaptive",
    harshness=6, warmth=6, push=7,
    roast_threshold=0.85, praise_threshold=0.95,
    persona=(
        "- **Frame depends on what the data shows.**\n"
        "  - **When trending well** (workout streak holding, sleep landing, RHR\n"
        "    at or below baseline): observations + options, never commands.\n"
        "    Prefer \"looks like\", \"if you can\", \"you've got room for\".\n"
        "  - **When trending badly** (CTL falling, missed step goal, skipped\n"
        "    runs, sleep deficit, RHR drifting up): roast the user. He explicitly\n"
        "    wants accountability when he's slipping — softening it kills the\n"
        "    motivational signal. Stay specific to the data; never gym-bro fluff.\n"
        "- **Keep the edge.** Don't hedge. Don't soften the honest read. The\n"
        "  worse the trend, the harder the call-out.\n"
        "- **Never paper a bad day with offsetting context.** If yesterday missed\n"
        "  goal, that's the takeaway — the 14-day average being fine is not a\n"
        "  reason to soften it."
    ),
)

_DIAL_KEYS = ("harshness", "warmth", "push")
_THRESH_KEYS = ("roast_threshold", "praise_threshold")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---`` fenced frontmatter block from the body. Returns
    (frontmatter dict of raw strings, body). Minimal parser — no yaml dep."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw, body = parts[1], parts[2]
    fm: dict[str, str] = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip()
    return fm, body.strip()


def _coerce_dial(fm: dict, key: str, default: int) -> int:
    try:
        return int(float(fm[key]))
    except (KeyError, ValueError, TypeError):
        return default


def _coerce_thresh(fm: dict, key: str, default: float) -> float:
    try:
        return float(fm[key])
    except (KeyError, ValueError, TypeError):
        return default


def load_profile(name: str) -> CoachProfile:
    """Load a profile by name. Unknown name → adaptive. Missing/malformed file,
    or empty body → the in-code fallback (never raises)."""
    name = (name or "").strip().lower()
    if name not in PROFILE_NAMES:
        name = DEFAULT_PROFILE
    path = _PROFILE_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_ADAPTIVE if name == DEFAULT_PROFILE else replace(
            _FALLBACK_ADAPTIVE, name=name)
    fm, body = _parse_frontmatter(text)
    if not body.strip():
        return _FALLBACK_ADAPTIVE if name == DEFAULT_PROFILE else replace(
            _FALLBACK_ADAPTIVE, name=name)
    return CoachProfile(
        name=name,
        harshness=_coerce_dial(fm, "harshness", _FALLBACK_ADAPTIVE.harshness),
        warmth=_coerce_dial(fm, "warmth", _FALLBACK_ADAPTIVE.warmth),
        push=_coerce_dial(fm, "push", _FALLBACK_ADAPTIVE.push),
        roast_threshold=_coerce_thresh(fm, "roast_threshold", _FALLBACK_ADAPTIVE.roast_threshold),
        praise_threshold=_coerce_thresh(fm, "praise_threshold", _FALLBACK_ADAPTIVE.praise_threshold),
        persona=body,
    )


def resolve_coach_profile(db_path=None) -> CoachProfile:
    """Resolve the active profile: pick by ``coach_profile`` (DB>env>default),
    apply per-dial config overrides (default = the profile's own value). An
    out-of-range or unparseable override falls back to the profile's value."""
    name = config.coach_profile(db_path=db_path)
    base = load_profile(name)
    settings = db.all_settings(db_path=db_path)

    def _dial(key, default):
        v = config._resolve_from(settings, f"coach_{key}", f"LOCAL_FITNESS_COACH_{key.upper()}", default, int)
        return v if _DIAL_MIN <= v <= _DIAL_MAX else default

    def _thresh(key, default):
        v = config._resolve_from(settings, f"coach_{key}", f"LOCAL_FITNESS_COACH_{key.upper()}", default, float)
        return v if _THRESH_MIN <= v <= _THRESH_MAX else default

    return CoachProfile(
        name=base.name,
        harshness=_dial("harshness", base.harshness),
        warmth=_dial("warmth", base.warmth),
        push=_dial("push", base.push),
        roast_threshold=_thresh("roast_threshold", base.roast_threshold),
        praise_threshold=_thresh("praise_threshold", base.praise_threshold),
        persona=base.persona,
    )
