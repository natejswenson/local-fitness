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


def _db_setting(key, db_path=None, conn: sqlite3.Connection | None = None):
    """Read the DB layer, treating an unreachable database as UNSET.

    A fresh clone has no ``data/fitness.db`` until the first ``fitness pull``,
    and ``db.get_setting`` raises ``OperationalError: no such table: settings``
    against an empty or absent one. CLAUDE.md's rule is that a stranger's clone
    must run, and every knob resolved here has a working env/default layer
    underneath — so an unreadable DB is a reason to fall through, never to
    crash.

    This was a live bug, not a hypothetical: ``mailer.load_config`` started
    reading a setting (0.51.0) and took down ``fitness brief-email`` on any
    machine without an initialized DB. It passed locally and failed in CI,
    which is exactly the shape this fail-open prevents — the same fresh-clone
    fail-open ``mcp_server._with_coach_persona`` already keeps for its cache
    key.

    Scoped to sqlite/OS errors: a bug in the *caller* (a bad key type, say)
    still raises, because that is not a missing-database condition.
    """
    try:
        return db.get_setting(key, db_path=db_path, conn=conn)
    except (sqlite3.Error, OSError):
        return None


def _resolve(key, env, default, cast, db_path=None, conn: sqlite3.Connection | None = None):
    """Resolve a single knob (own DB read). For standalone single-knob use.

    Accepts an already-open ``conn`` to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    return _coerce(
        _db_setting(key, db_path=db_path, conn=conn), os.environ.get(env), default, cast
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
    """Selected coach tone profile name (DB > env > default 'hardass'). The
    returned name is whitelisted downstream by coach.load_profile. The default
    literal must equal coach.DEFAULT_PROFILE — config can't import coach
    (cycle), so tests/test_config.py pins the pair together.

    Accepts an already-open ``conn`` (threaded through to ``_resolve``, the
    function that actually reads the DB) to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    return _resolve("coach_profile", "LOCAL_FITNESS_COACH_PROFILE",
                    "hardass", _as_profile_name, db_path, conn=conn)


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


def brief_email_enabled(db_path=None, conn: sqlite3.Connection | None = None) -> bool:
    """Whether the evening job sends (DB > env > default True).

    The conversational kill switch — `update_brief_email_settings(enabled=
    false)` writes the DB layer, so "stop emailing me the brief" needs neither
    a text editor nor `launchctl`. Defaulting to True is safe for a fresh
    clone because sending ALSO requires an SMTP password, which has no default
    at any layer; a stranger who never sets one can never be mailed by this.
    """
    return _resolve("brief_email_enabled", "LOCAL_FITNESS_BRIEF_EMAIL_ENABLED",
                    True, _as_bool, db_path, conn=conn)


def _as_recipients(s) -> tuple[str, ...]:
    """Parse a comma-separated recipient list, dropping blanks.

    Raises on an empty result so ``_coerce`` falls through to the default
    rather than returning an empty tuple — "configured to mail nobody" is a
    silent no-send, and the fallback (the sending account) is the safer read
    of a malformed value."""
    out = tuple(a.strip() for a in str(s).split(",") if a.strip())
    if not out:
        raise ValueError("no recipients")
    return out


def brief_email_to(db_path=None, conn: sqlite3.Connection | None = None) -> tuple[str, ...]:
    """Who the evening brief goes to (DB > env > default: empty).

    Empty means "not configured" and lets ``mailer.load_config`` fall back to
    the sending account — the resolution can't do that itself without reading
    SMTP settings, and this module stays free of them so it holds no
    credential-adjacent state."""
    return _resolve("brief_email_to", "LOCAL_FITNESS_BRIEF_EMAIL_TO",
                    (), _as_recipients, db_path, conn=conn)


def plan_calendar_enabled(db_path=None, conn: sqlite3.Connection | None = None) -> bool:
    """Whether the evening job writes tomorrow's session to Google Calendar
    (DB > env > default True).

    The conversational kill switch, exactly like ``brief_email_enabled``:
    `update_plan_calendar_settings(enabled=false)` writes the DB layer, so
    "stop putting runs on my calendar" needs neither a text editor nor
    `launchctl`. Defaulting to True is safe for a fresh clone because writing
    ALSO requires an OAuth refresh token, which has no default at any layer; a
    stranger who never runs `fitness calendar-auth` can never have an event
    created by this.
    """
    return _resolve("plan_calendar_enabled", "LOCAL_FITNESS_PLAN_CALENDAR_ENABLED",
                    True, _as_bool, db_path, conn=conn)


def _as_calendar_id(s) -> str:
    """Normalize a calendar id (strip only). Not lowercased: a calendar id is
    usually an email address, and while the domain is case-insensitive the
    local part is not — Google matches it verbatim."""
    out = str(s).strip()
    if not out:
        raise ValueError("empty calendar id")
    return out


def plan_calendar_id(db_path=None, conn: sqlite3.Connection | None = None) -> str:
    """Which calendar tomorrow's session lands on (DB > env > 'primary').

    ``primary`` is the authenticated account's own default calendar, which is
    what the OAuth flow authorizes — so the default works with no configuration
    at all. Set it to a specific calendar id only to keep training off the main
    grid.
    """
    return _resolve("plan_calendar_id", "LOCAL_FITNESS_PLAN_CALENDAR_ID",
                    "primary", _as_calendar_id, db_path, conn=conn)


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
