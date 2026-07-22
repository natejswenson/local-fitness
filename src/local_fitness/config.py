"""User-tunable behavioral config for grading and projection.

Resolution precedence per knob: **settings DB > env var > hardcoded default**.
Every default equals the value that was previously hardcoded, so a fresh clone
(no settings, no env) behaves identically. Set a value live with
``fitness config set <key> <value>`` (DB layer) or in ``.env`` (env layer).

A blank (empty / whitespace-only) value at any layer is treated as UNSET so it
falls through to the next layer; an unrecognized value falls back to the
default. This module is the single home for reading the knobs; the pure grading
functions in ``plans.py`` never call it — they receive a resolved
``GradingConfig`` from their callers.
"""
from __future__ import annotations

import os
import sqlite3

from . import db

# --- defaults (equal to the previously-hardcoded values) -------------------

DEFAULT_DONE_FRACTION = 0.80
DEFAULT_PARTIAL_FRACTION = 0.40
DEFAULT_COUNT_WALKS_EASY = True
DEFAULT_COUNT_WALKS_MILEAGE = False
DEFAULT_RIEGEL_LOOKBACK_DAYS = 120
#: Display name when nothing is configured. Deliberately generic — a real name
#: must never be a default in tracked code (CLAUDE.md's env-driven rule), and
#: this is the constant ``prompts.DEFAULT_USER_NAME`` aliases.
DEFAULT_USER_NAME = "the user"
_RIEGEL_LOOKBACK_MAX_DAYS = 3650  # ~10 years; guards against nonsense windows

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _blank(raw) -> bool:
    """An empty- or whitespace-only stored value (DB or env) means UNSET."""
    return raw is not None and str(raw).strip() == ""


def _as_bool(s) -> bool:
    """Strict bool parse: only known tokens; anything else raises so the caller
    falls back to the default rather than silently flipping to False."""
    tok = str(s).strip().lower()
    if tok in _BOOL_TRUE:
        return True
    if tok in _BOOL_FALSE:
        return False
    raise ValueError(f"not a recognized bool: {s!r}")


def _coerce(db_raw, env_raw, default, cast):
    """Apply DB > env > default precedence with blank-normalization + safe cast."""
    raw = None if _blank(db_raw) else db_raw
    if raw is None:
        raw = None if _blank(env_raw) else env_raw
    if raw is None:
        return default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        return default


def _resolve(key, env, default, cast, db_path=None, conn: sqlite3.Connection | None = None):
    """Resolve a single knob (own DB read). For standalone single-knob use.

    Accepts an already-open ``conn`` to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    return _coerce(
        db.get_setting(key, db_path=db_path, conn=conn), os.environ.get(env), default, cast
    )


def _resolve_from(settings: dict, key, env, default, cast):
    """Resolve a knob against an already-fetched settings dict (batched)."""
    return _coerce(settings.get(key), os.environ.get(env), default, cast)


# --- standalone accessors (single-knob; the grading path uses the batched
#     resolve_grading_config in plans.py instead) -----------------------------

def _as_profile_name(s) -> str:
    """Normalize a coach-profile name (lowercase/strip). The whitelist check
    lives in coach.load_profile (unknown → adaptive), so this never raises —
    keeping config free of a coach import (avoids a config↔coach cycle)."""
    return str(s).strip().lower()


def coach_profile(db_path=None, conn: sqlite3.Connection | None = None) -> str:
    """Selected coach tone profile name (DB > env > default 'adaptive'). The
    returned name is whitelisted downstream by coach.load_profile.

    Accepts an already-open ``conn`` (threaded through to ``_resolve``, the
    function that actually reads the DB) to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    return _resolve("coach_profile", "LOCAL_FITNESS_COACH_PROFILE",
                    "adaptive", _as_profile_name, db_path, conn=conn)


def _as_user_name(s) -> str:
    """Collapse whitespace on a display name; a blank falls through to the
    default via ``_coerce``'s ``_blank`` check."""
    return " ".join(str(s).split())


def user_name(db_path=None, conn: sqlite3.Connection | None = None) -> str:
    """Who the coach is talking to (DB > env > default 'the user').

    The single resolver for the display name, mirroring ``coach_profile``
    above. It exists because there wasn't one: ``briefing.py`` and
    ``web/mcp_server.py`` each called ``db.get_setting("user_name", ...)``
    directly with one default, ``brief_planner`` called it with a DIFFERENT
    default, and ``plan_coach``/``workout_coach`` hardcoded a name into their
    prompt text outright. Same setting, four behaviors — and the hardcoded one
    meant a stranger's clone was told it was Nate's coach.

    Defaulting to a generic ``the user`` (never a real name) is what keeps
    tracked code free of personal data, per CLAUDE.md's env-driven rule."""
    return _resolve("user_name", "LOCAL_FITNESS_USER_NAME",
                    DEFAULT_USER_NAME, _as_user_name, db_path, conn=conn)


def riegel_lookback_days(db_path=None, conn: sqlite3.Connection | None = None) -> int:
    """Lookback window (days) for the projected-finish best effort. Clamps a
    nonsense value (< 1 or > ~10 years) to the default.

    Accepts an already-open ``conn`` (threaded through to ``_resolve``, the
    function that actually reads the DB) to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    n = _resolve("riegel_lookback_days", "LOCAL_FITNESS_RIEGEL_LOOKBACK_DAYS",
                 DEFAULT_RIEGEL_LOOKBACK_DAYS, int, db_path, conn=conn)
    return n if 1 <= n <= _RIEGEL_LOOKBACK_MAX_DAYS else DEFAULT_RIEGEL_LOOKBACK_DAYS
