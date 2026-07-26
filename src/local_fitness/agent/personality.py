"""The tunable personality spec — the DB-stored, conversationally-edited voice.

The profile ``.md`` files (``coach_profiles/``) are the SEEDS: what a fresh
clone speaks with before any tuning. The first ``update_coach_personality``
call materializes a spec from the active profile plus the requested patch into
the settings table (key :data:`SPEC_KEY`), and from then on the spec is the
single source of truth for the persona prose — every voice surface composes
``CoachProfile.effective_persona``, which prefers the spec. Editing is
agent-owned (MCP tools), the same write model as training plans; nothing here
is ever hand-edited on disk.

Design constraints:
  * **No coach.py import** — ``coach`` imports this module; the profile
    parameter of :func:`seed_from_profile` is duck-typed.
  * **Riding the existing settings read.** ``resolve_coach_profile`` already
    fetches ``db.all_settings()`` once per resolution; the spec is parsed out
    of that same dict — zero added connections, which matters because
    resolution runs at MCP connect time and before every render.
  * **Fail-open.** A malformed stored spec logs and resolves to ``None`` (the
    profile file speaks); it is never deleted by a read path.
  * **Bounded.** Serialized cap 8 KB, identity 4000 chars, 12 items per list,
    16 intensity topics — the spec feeds every prompt, so its token cost must
    be bounded by construction, like the journal's.

``LOCAL_FITNESS_COACH_SPEC=0`` ignores the stored spec entirely (instant
rollback of a bad tune without deleting it).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

_LOG = logging.getLogger(__name__)

SPEC_KEY = "coach_personality_spec"
SPEC_VERSION = 1

SPEC_MAX_BYTES = 8192
IDENTITY_MAX_CHARS = 4000
LIST_MAX_ITEMS = 12
CATCHPHRASE_MAX_CHARS = 120
LIST_ITEM_MAX_CHARS = 200
MAX_TOPICS = 16

INTENSITY_LEVELS = ("off", "low", "medium", "high", "brutal")

#: Topics the coach already talks about, named so a tune like "ease up about
#: the step goal" has a stable key. Custom slugs are allowed (the coach may
#: grow new topics conversationally) but must look like slugs.
TOPIC_WHITELIST = frozenset({
    "step_goal_nagging", "quality_day_misses", "plan_adherence", "sleep",
    "recovery", "conditioning", "praise", "excuses",
})
TOPIC_SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")

#: How each intensity level reads as an instruction. Deterministic text, so a
#: tuned topic produces the same prompt line every time (cache stability).
_INTENSITY_INSTRUCTIONS = {
    "off": "do not bring this up unprompted",
    "low": "mention only when it genuinely changes today's call, briefly",
    "medium": "normal treatment per your profile",
    "high": "lean into this — call it out whenever the data shows it",
    "brutal": "maximum pressure — never let a miss here pass without naming it",
}


@dataclass(frozen=True)
class PersonalitySpec:
    version: int = SPEC_VERSION
    base_profile: str = ""
    identity: str = ""
    catchphrases: tuple[str, ...] = ()
    principles: tuple[str, ...] = ()
    never_do: tuple[str, ...] = ()
    intensity: Mapping[str, str] = field(default_factory=dict)
    updated_at: str | None = None


def spec_enabled() -> bool:
    return os.environ.get(
        "LOCAL_FITNESS_COACH_SPEC", "1"
    ).strip().lower() not in ("0", "false", "no", "off")


def _clean_str_tuple(value, max_items: int, max_chars: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:max_chars])
        if len(out) >= max_items:
            break
    return tuple(out)


def parse_spec(raw_json: str | None) -> PersonalitySpec | None:
    """The stored spec, or ``None`` (absent, malformed, oversized, disabled).
    Fail-open by contract: a read path never raises and never deletes."""
    if not raw_json or not spec_enabled():
        return None
    if len(raw_json.encode("utf-8", errors="replace")) > SPEC_MAX_BYTES:
        _LOG.warning("coach personality spec exceeds %d bytes — ignoring",
                     SPEC_MAX_BYTES)
        return None
    try:
        data = json.loads(raw_json)
        if not isinstance(data, dict):
            raise ValueError("spec is not an object")
        identity = data.get("identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("spec has no identity prose")
        intensity = {}
        raw_intensity = data.get("intensity") or {}
        if isinstance(raw_intensity, dict):
            for topic, level in raw_intensity.items():
                if (isinstance(topic, str) and TOPIC_SLUG_RE.match(topic)
                        and level in INTENSITY_LEVELS):
                    intensity[topic] = level
                if len(intensity) >= MAX_TOPICS:
                    break
        return PersonalitySpec(
            version=int(data.get("version") or SPEC_VERSION),
            base_profile=str(data.get("base_profile") or "").strip().lower(),
            identity=identity.strip()[:IDENTITY_MAX_CHARS],
            catchphrases=_clean_str_tuple(
                data.get("catchphrases"), LIST_MAX_ITEMS, CATCHPHRASE_MAX_CHARS),
            principles=_clean_str_tuple(
                data.get("principles"), LIST_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
            never_do=_clean_str_tuple(
                data.get("never_do"), LIST_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
            intensity=intensity,
            updated_at=data.get("updated_at"),
        )
    except (ValueError, TypeError):
        _LOG.warning("coach personality spec is malformed — ignoring",
                     exc_info=True)
        return None


def spec_to_json(spec: PersonalitySpec) -> str:
    return json.dumps({
        "version": spec.version,
        "base_profile": spec.base_profile,
        "identity": spec.identity,
        "catchphrases": list(spec.catchphrases),
        "principles": list(spec.principles),
        "never_do": list(spec.never_do),
        "intensity": dict(spec.intensity),
        "updated_at": spec.updated_at,
    }, ensure_ascii=False)


def seed_from_profile(profile) -> PersonalitySpec:
    """A spec whose rendered persona is exactly the profile's own prose — the
    starting point the first conversational tune patches. ``profile`` is a
    ``coach.CoachProfile`` (duck-typed: ``.name`` + ``.persona``)."""
    return PersonalitySpec(base_profile=profile.name, identity=profile.persona)


_PATCH_LIST_OPS = {
    "add_catchphrase": ("catchphrases", CATCHPHRASE_MAX_CHARS),
    "remove_catchphrase": ("catchphrases", CATCHPHRASE_MAX_CHARS),
    "add_principle": ("principles", LIST_ITEM_MAX_CHARS),
    "remove_principle": ("principles", LIST_ITEM_MAX_CHARS),
    "add_never_do": ("never_do", LIST_ITEM_MAX_CHARS),
    "remove_never_do": ("never_do", LIST_ITEM_MAX_CHARS),
}
PATCH_FIELDS = frozenset({"identity", "set_intensity", *_PATCH_LIST_OPS})


def validate_patch(patch: dict) -> tuple[dict, list[str]]:
    """(clean patch, errors). Unknown fields, bad levels, bad slugs and
    over-caps are ERRORS (echoed to the agent), never silent drops — the
    agent is mid-conversation and can fix its call."""
    clean: dict = {}
    errors: list[str] = []
    for key, value in patch.items():
        if key not in PATCH_FIELDS:
            errors.append(f"unknown field '{key}'")
            continue
        if key == "identity":
            if not isinstance(value, str) or not value.strip():
                errors.append("identity must be non-empty prose")
            elif len(value) > IDENTITY_MAX_CHARS:
                errors.append(
                    f"identity too long ({len(value)} chars, "
                    f"max {IDENTITY_MAX_CHARS})")
            else:
                clean[key] = value.strip()
        elif key == "set_intensity":
            if not isinstance(value, dict) or not value:
                errors.append("set_intensity must be a non-empty object of "
                              "topic -> level")
                continue
            levels = {}
            for topic, level in value.items():
                if not isinstance(topic, str) or not TOPIC_SLUG_RE.match(topic):
                    errors.append(
                        f"bad intensity topic '{topic}' (want a slug like "
                        f"'step_goal_nagging')")
                elif level not in INTENSITY_LEVELS:
                    errors.append(
                        f"bad intensity level '{level}' for '{topic}' "
                        f"(one of {list(INTENSITY_LEVELS)})")
                else:
                    levels[topic] = level
            if levels:
                clean[key] = levels
        else:  # list add/remove
            _field, max_chars = _PATCH_LIST_OPS[key]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} must be a non-empty string")
            elif key.startswith("add_") and len(value.strip()) > max_chars:
                errors.append(
                    f"{key} too long ({len(value.strip())} chars, "
                    f"max {max_chars})")
            else:
                clean[key] = value.strip()
    return clean, errors


def apply_patch(spec: PersonalitySpec, patch: dict) -> PersonalitySpec:
    """A new spec with a VALIDATED patch applied. Add is idempotent (no dup
    lines), remove matches case-insensitively, intensity merges per topic
    (``medium`` returns a topic to profile-default and drops the override)."""
    updates: dict = {}
    if "identity" in patch:
        updates["identity"] = patch["identity"][:IDENTITY_MAX_CHARS]
    for key, value in patch.items():
        if key not in _PATCH_LIST_OPS:
            continue
        field_name, _ = _PATCH_LIST_OPS[key]
        current = list(updates.get(field_name, getattr(spec, field_name)))
        if key.startswith("add_"):
            if value.lower() not in {c.lower() for c in current}:
                current.append(value)
            updates[field_name] = tuple(current[:LIST_MAX_ITEMS])
        else:
            updates[field_name] = tuple(
                c for c in current if c.lower() != value.lower())
    if "set_intensity" in patch:
        merged = dict(spec.intensity)
        for topic, level in patch["set_intensity"].items():
            if level == "medium":
                merged.pop(topic, None)
            else:
                merged[topic] = level
        # Cap by insertion order — oldest overrides win, the agent is told.
        updates["intensity"] = dict(list(merged.items())[:MAX_TOPICS])
    return replace(spec, **updates)


def render_spec_persona(spec: PersonalitySpec) -> str:
    """The persona prose a tuned profile speaks with. Deterministic: identical
    specs render identical text (the PDF coaches hash their prompts)."""
    parts = [spec.identity]
    if spec.principles:
        parts.append("## Principles\n" + "\n".join(
            f"- {p}" for p in spec.principles))
    if spec.catchphrases:
        parts.append(
            "## Signature lines (use sparingly — at most one per brief, "
            "never two in one reply)\n"
            + "\n".join(f'- "{c}"' for c in spec.catchphrases))
    if spec.intensity:
        lines = [
            f"- {topic}: {level} — {_INTENSITY_INSTRUCTIONS[level]}"
            for topic, level in spec.intensity.items()
        ]
        parts.append(
            "## Per-topic intensity (tuned by the user — these override the "
            "profile's default treatment)\n" + "\n".join(lines))
    if spec.never_do:
        parts.append("## Never do\n" + "\n".join(
            f"- {n}" for n in spec.never_do))
    return "\n\n".join(parts)
