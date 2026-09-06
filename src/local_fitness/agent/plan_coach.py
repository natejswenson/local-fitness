"""Claude-generated coaching line for the PDF report's Training Plan section.

Deliberately its own module rather than folded into ``briefing.py`` (owns
the whole daily-brief generation lifecycle, eval'd against
``baseline.json``/fixtures — a different concern and lifecycle) or
``visuals.py`` (pure rendering, no LLM calls, no DB access — stays that
way). The Claude Agent SDK is imported inside ``generate_coaching_line``'s
body, never at module scope: ``agent/tools.py`` (which imports this module)
is imported by the always-running containerized web server, and that
process must never pay the SDK's import cost for a PDF-only feature it
never uses — mirrors ``visuals.py``'s deferred matplotlib/weasyprint
imports for the same reason.

``briefing`` must NEVER be imported at module scope here, and is no longer
imported at all: ``briefing.py`` itself imports ``tools.py`` at module
scope (as ``agent_tools``, for the V1 monolith's MCP-tool wiring), and
``tools.py`` imports this module — a module-scope ``from . import
briefing`` here would close that into a real circular import (``tools ->
plan_coach -> briefing -> tools``) that breaks at process start. The
call-time import that used to sidestep it existed for one purpose, reading
``briefing.DEFAULT_MODEL``, and ``DEFAULT_MODEL`` below replaced it. The
prohibition outlives the import it explained.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

from .. import config
from . import grounding, prompts
from .coach import CoachProfile
from .grounding import GroundingFlag

_LOG = logging.getLogger(__name__)

#: This module's model, deliberately NOT ``briefing.DEFAULT_MODEL`` — the same
#: split ``workout_coach`` made, arrived at one bug later.
#:
#: That constant drives the eval'd daily-brief generator, where a model change
#: is a prompt change that has to clear the scorer, a cross-model A/B and the
#: invention-rate gate. Following it meant this call could never be tuned, and
#: it inherited the SDK defaults along with it: no ``effort``, no ``thinking``,
#: i.e. adaptive thinking at high effort. Measured on real evening briefs
#: (``logs/briefmail.launchd.err.log``, 2026-08-07 -> 2026-09-05): 23 of 30
#: nights hit the old 30 s ceiling and shipped ``fallback_coaching_line``
#: instead. Successes ran 9.3-26.6 s; failures pinned at the ceiling.
DEFAULT_MODEL = "claude-sonnet-5"

#: Low effort and thinking off — load-bearing, not polish, the same finding
#: ``workout_coach`` measured (median 142.9 s -> 10.0 s). The control is in
#: this app's own logs: on 2026-09-05 the brief composer wrote a whole
#: three-card brief on ``claude-sonnet-4-6`` in 64 s with ``effort="low"``,
#: 65 seconds before this module failed to write two sentences in 30 at
#: default effort. There is nothing here to reason about — ``plans.py``
#: decided every verdict in Python before the prompt was built.
DEFAULT_EFFORT = "low"

#: 45 s, matching ``reflect`` rather than ``workout_coach``'s 90 s: this is a
#: short generation inside a scheduled job with a backstop slot, where the
#: ceiling bounds what a bad night costs the job, not an interactive on-demand
#: render. It is ~4.5x the ~10 s the corrected config should take and sits
#: above the entire observed success tail under the OLD config (max 26.6 s),
#: so it recovers most of today's distribution even if the config change
#: under-delivers.
#:
#: It is not a guarantee, and no value here would be: the config bounds the
#: failure RATE, never the wall clock. Two logged stalls (plan_coach 519.7 s
#: against a 30 s ceiling, reflect 359 s against 45 s) blew past their
#: ceilings entirely — ``asyncio.wait_for`` fired and cancellation did not
#: return for minutes. The fallback stays the safety net for that residual;
#: ``coaching_line_source`` is what makes the residual measurable.
DEFAULT_TIMEOUT_S = 45.0

# Verdict wording is date-relative: the graded day being referred to can be
# the report's own date (an evening re-render after today's run synced and
# graded), yesterday, or — when the data frontier lags — several days back.
# A single hardcoded "Yesterday" mislabels the first and third cases, and the
# fallback's whole contract is that only WORDING degrades, never facts.
_VERDICT_PHRASE_TODAY = {
    "done": "Today's session is already in the book.",
    "partial": "Today's session came up short of the prescription.",
    "missed": "Today's session is a skip so far.",
    "compliant": "Today is a scheduled rest day.",
}
# ``{ref}`` is "Yesterday" or an absolute date like "Jul 8".
_VERDICT_PHRASE_PRIOR = {
    "done": "{ref} you hit the session clean.",
    "partial": "{ref} came up short of the prescription.",
    "missed": "{ref} was a skip.",
    "compliant": "{ref} was a scheduled rest day.",
}


def _relative_day(prior_date: str | None, target_date: str | None) -> str:
    """How to refer to a graded day relative to the report's date: ``"Today"``,
    ``"Yesterday"``, or an absolute ``"Jul 8"``. Falls back to the absolute
    date whenever the relationship can't be computed (missing/malformed date,
    no target), so the wording is never *wrong* about which day it means — only
    less familiar. Returns ``""`` only when there is no usable prior date at
    all, in which case the caller omits the verdict phrase entirely."""
    if not prior_date:
        return ""
    try:
        pd = date.fromisoformat(prior_date)
    except (ValueError, TypeError):
        return ""
    if target_date:
        try:
            delta = (date.fromisoformat(target_date) - pd).days
        except (ValueError, TypeError):
            delta = None
        if delta is not None:
            if delta <= 0:
                return "Today"
            if delta == 1:
                return "Yesterday"
    return f"{pd:%b} {pd.day}"

# Mirrors system_prompt's "Translate technical metrics on first use" bullet
# (prompts.py) — always included so the PDF coaching line honors the same
# metric-translation convention as chat/brief, regardless of whether any
# metric abbreviation actually appears in this particular line.
_METRIC_TRANSLATION_BLOCK = (
    "Translate technical metrics on first use, the same way you always do: "
    'CTL -> "fitness" (training base over the last six weeks), '
    'ATL -> "fatigue" (load from the last 7 days), '
    'TSB -> "freshness" (positive = rested, negative = worn down). '
    "Pair every number with its plain-English meaning."
)


def _format_prescription(today_workout: dict) -> str:
    prescription = today_workout["type"]
    if today_workout.get("distance_mi") is not None:
        prescription += f" {today_workout['distance_mi']} mi"
    if today_workout.get("pace_min_per_mi"):
        prescription += f" @ {today_workout['pace_min_per_mi']}/mi"
    return prescription


def build_prompt(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
    sessions_adherence_pct: int | None = None,
) -> tuple[str, str]:
    """Assemble the ``(system_prompt, user_prompt)`` pair for the coaching
    line. Pure string assembly — no I/O, no randomness, fully unit-testable.

    ``notes_text`` is the caller's already-rendered
    ``notes.render_for_prompt()`` output (same pattern as
    ``prompts.system_prompt``, prompts.py:26-33) — this function does no I/O
    of its own. When provided (non-empty), it's appended as a notes section
    so a saved preference ("stop roasting my steps") is honored on the PDF
    coaching line exactly as it already is in chat/brief. The
    metric-translation block is appended unconditionally, independent of
    ``notes_text``.

    ``memory_text`` follows the identical convention for the coach's memory
    (``memory.render_memory_for_prompt()`` output) — passed in, not resolved,
    because this prompt's hash is ``generate_coaching_line_cached``'s cache
    key and a builder that did I/O would break caching.

    ``sessions_adherence_pct`` (``plans._sessions_adherence_pct``) is appended
    to the adherence line when supplied, so the coach reads the number that
    excludes rest days alongside the one that counts them — a plan with three
    rest days a week can show 80%+ overall while half the running went
    undone. Optional and ``None`` by default: omitting it reproduces the
    previous prompt byte for byte, so an un-wired caller keeps its cache.
    """
    system_prompt = (
        f"You are {user_name}'s running coach, writing ONE short paragraph (2-4 "
        "sentences, no more) that preps him for today's prescribed run.\n\n"
        f"{prompts.coach_voice_block(user_name, profile)}\n\n"
        f"{_METRIC_TRANSLATION_BLOCK}\n\n"
        "Output ONLY the coaching paragraph itself — no headline, no "
        'markdown, no quotation marks, no preamble like "Here\'s your line".'
    )
    memory_section = prompts.coach_memory_block(user_name, memory_text)
    if memory_section:
        system_prompt += f"\n\n{memory_section}"
    notes_section = prompts.user_notes_block(user_name, notes_text)
    if notes_section:
        system_prompt += f"\n\n{notes_section}"

    lines = [f"Today's prescribed workout: {_format_prescription(today_workout)}."]
    if today_workout.get("description"):
        lines.append(f"Prescription notes: {today_workout['description']}")

    adherence_line = f"Plan adherence over the last graded stretch: {adherence_pct}%."
    if sessions_adherence_pct is not None:
        adherence_line += (
            f" Excluding rest days, {sessions_adherence_pct}% of prescribed sessions."
        )
    lines.append(adherence_line)
    if days_to_race is not None:
        lines.append(f"{days_to_race} days to the {goal_type}.")
    else:
        lines.append(f"Goal: {goal_type}.")

    if last_7_days:
        lines.append("Last 7 days, most recent first:")
        for day in last_7_days:
            planned = f"{day['planned_mi']} mi" if day.get("planned_mi") is not None else "—"
            actual = f"{day['actual_mi']} mi" if day.get("actual_mi") is not None else "—"
            lines.append(
                f"  {day['date']}: {day['type']} — planned {planned}, "
                f"actual {actual}, verdict {day['verdict']}"
            )

    return system_prompt, "\n".join(lines)


async def generate_coaching_line(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
    sessions_adherence_pct: int | None = None,
) -> str:
    """Claude-generated coaching line prepping the athlete for today's run.

    Raises on any failure (missing/expired credential, network, timeout,
    empty response) — the caller (``tools.generate_brief_report``) is
    responsible for falling back to ``fallback_coaching_line``. ``model``
    (``None`` by default) resolves to this module's own ``DEFAULT_MODEL``;
    see that constant for why it is no longer ``briefing.DEFAULT_MODEL``.
    ``notes_text`` is plumbed straight through to ``build_prompt`` — see
    its docstring for the notes-parity rationale.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    if model is None:
        model = DEFAULT_MODEL

    system_prompt, user_prompt = build_prompt(
        profile, today_workout, last_7_days, adherence_pct, days_to_race, goal_type,
        notes_text=notes_text, user_name=user_name, memory_text=memory_text,
        sessions_adherence_pct=sessions_adherence_pct,
    )
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=1,
        # See DEFAULT_EFFORT: without these, the model ID alone is a latency
        # regression rather than a fix — this is what timed out 23 of 30 nights.
        effort=DEFAULT_EFFORT,
        thinking={"type": "disabled"},
    )

    async def _run() -> str:
        chunks: list[str] = []
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks).strip()

    text = await asyncio.wait_for(_run(), timeout=timeout)
    if not text:
        raise RuntimeError("coaching-line generator returned an empty response")
    return text


def _cache_path() -> Path:
    """The single-entry coaching-line cache, kept next to the SQLite DB (the
    per-deployment data directory — already gitignored, already the one
    host/container-shared writable location)."""
    from .. import db  # lazy: keep module import-cost near zero (module docstring)

    return db.DEFAULT_DB_PATH.parent / "plan_coach_cache.json"


#: Multi-entry cache cap (0.36.0). The old single-entry "latest key wins"
#: shape thrashed the moment two different brief dates alternated — every
#: render of A evicted B and vice versa, each miss a live SDK call on
#: ``DEFAULT_TIMEOUT_S``. 32 entries is a month of dates with room for plan
#: edits.
CACHE_MAX_ENTRIES = 32


def _load_cache_entries(path: Path) -> dict[str, dict]:
    """The cache file's entries, tolerating every historical shape: the v2
    ``{"version": 2, "entries": {...}}`` dict, the retired v1 single-entry
    ``{"key": ..., "line": ...}`` (read as one entry so an upgrade keeps its
    hit), and missing/corrupt files (empty). Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") == 2 and isinstance(data.get("entries"), dict):
        return {
            k: v for k, v in data["entries"].items()
            if isinstance(v, dict) and isinstance(v.get("line"), str) and v["line"]
        }
    if isinstance(data.get("key"), str) and isinstance(data.get("line"), str):
        return {data["key"]: {"line": data["line"], "ts": ""}}
    return {}


def _read_cached_line(path: Path, key: str) -> str | None:
    """The cached line for exactly ``key``, else None. Never raises."""
    entry = _load_cache_entries(path).get(key)
    return entry["line"] if entry else None


def _write_cached_line(path: Path, key: str, line: str) -> None:
    """Best-effort v2 multi-entry cache write, capped at
    ``CACHE_MAX_ENTRIES`` by evicting the oldest ``ts``. A cache write
    failure must never fail the PDF render — swallow and log."""
    try:
        entries = _load_cache_entries(path)
        entries[key] = {
            "line": line,
            "ts": datetime.now(UTC).isoformat(),
        }
        while len(entries) > CACHE_MAX_ENTRIES:
            # Missing ts (a migrated v1 entry) sorts oldest — first out.
            oldest = min(entries, key=lambda k: entries[k].get("ts") or "")
            del entries[oldest]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": 2, "entries": entries}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOG.warning("plan_coach cache write failed (ignored)", exc_info=True)


async def generate_coaching_line_cached(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
    sessions_adherence_pct: int | None = None,
    cache_path: Path | None = None,
) -> str:
    """``generate_coaching_line`` behind a single-entry disk cache.

    The 2026-07-19 facet review counted 9 PDF renders on one day with
    byte-identical inputs — 9 separate LLM round-trips for the same one-line
    paragraph. ``build_prompt`` is pure, so the (system, user) prompt pair
    fully captures every input (prescription, week, adherence, notes, voice);
    its hash is the cache key. Same key → the cached line, no SDK call; any
    input change (new day, edited plan, new note) → a fresh generation. Only
    successful generations are cached — a fallback line is never stored, so a
    transient SDK failure doesn't pin a template line for the rest of the day.
    """
    system_prompt, user_prompt = build_prompt(
        profile, today_workout, last_7_days, adherence_pct, days_to_race,
        goal_type, notes_text=notes_text, user_name=user_name,
        memory_text=memory_text, sessions_adherence_pct=sessions_adherence_pct,
    )
    key = hashlib.sha256(
        "\x00".join([system_prompt, user_prompt, model or "default"]).encode("utf-8")
    ).hexdigest()
    path = cache_path or _cache_path()
    cached = _read_cached_line(path, key)
    if cached is not None:
        _LOG.info("plan_coach cache hit — reusing coaching line")
        return cached
    line = await generate_coaching_line(
        profile, today_workout, last_7_days, adherence_pct, days_to_race,
        goal_type, model=model, timeout=timeout, notes_text=notes_text,
        user_name=user_name, memory_text=memory_text,
        sessions_adherence_pct=sessions_adherence_pct,
    )
    _write_cached_line(path, key, line)
    return line


def fallback_coaching_line(
    today_workout: dict,
    last_7_days: list[dict],
    days_to_race: int | None,
    goal_type: str,
    target_date: str | None = None,
) -> str:
    """Deterministic, template-based coaching line — used only when
    ``generate_coaching_line`` fails. Pure: identical inputs always
    produce identical output. Never raises.

    Deliberately restates NEITHER the prescription nor the description: the
    PDF's Today callout already prints both directly above this line, so
    including them rendered the same instruction three times over
    ("easy · 4.0 mi @ 9:39/mi" / "Easy 4mi. Keep HR under 140." / "Today: easy
    4.0 mi @ 9:39/mi. Easy 4mi. Keep HR under 140."). A coaching line's job is
    the part the prescription does not already say.

    ``target_date`` (the report's own date) makes the graded-day reference
    correct: ``last_7_days`` includes ``target_date`` itself, and a run that
    has already synced+graded ``done`` on the report's date is the first
    non-pending entry — so a hardcoded "Yesterday" credited today's run to
    yesterday. When omitted, the day is named by its absolute date, which is
    never wrong about which day it means."""
    prior = next((d for d in last_7_days if d.get("verdict") != "pending"), None)
    parts: list[str] = []
    if prior is not None:
        ref = _relative_day(prior.get("date"), target_date)
        verdict = prior["verdict"]
        if ref == "Today":
            phrase = _VERDICT_PHRASE_TODAY.get(verdict)
        elif ref:
            template = _VERDICT_PHRASE_PRIOR.get(verdict)
            phrase = template.format(ref=ref) if template else None
        else:
            phrase = None  # no usable date — omit rather than assert a wrong day
        if phrase:
            parts.append(phrase)

    if days_to_race is not None:
        parts.append(f"{days_to_race} days to your {goal_type}.")
    else:
        parts.append(f"Working toward your {goal_type}.")

    return " ".join(parts)


def _plan_section_pool(plan_section: dict) -> list[tuple[float, str]]:
    """The citable-number pool for ``ground_coaching_line``, built from the
    deterministic plan section — the same shape ``tools._build_plan_section``
    returns. Mirrors ``grounding._grounded_pool``'s (magnitude, source-name)
    shape so ``grounding.classify_against_pool`` works unmodified.

    Pace is a string ("9:23"/mi) — tokenized numerically (9, 23) the same way
    ``grounding._display_numbers`` tokenizes a GroundedValue's ``display``
    string, per the design's tokenizer-false-positive caveat.
    """
    pool: list[tuple[float, str]] = []

    adherence_pct = plan_section.get("adherence_pct")
    if adherence_pct is not None:
        pool.append((abs(float(adherence_pct)), "adherence_pct"))

    # Cited only when the caller wired it (build_prompt appends it to the
    # adherence line under the same condition) — a number the line can't have
    # seen must not become a licence to invent one.
    sessions_adherence_pct = plan_section.get("sessions_adherence_pct")
    if sessions_adherence_pct is not None:
        pool.append((abs(float(sessions_adherence_pct)), "sessions_adherence_pct"))

    days_to_race = plan_section.get("days_to_race")
    if days_to_race is not None:
        pool.append((abs(float(days_to_race)), "days_to_race"))

    today = plan_section.get("today") or {}
    distance_mi = today.get("distance_mi")
    if distance_mi is not None:
        pool.append((abs(float(distance_mi)), "today_distance_mi"))
    pace = today.get("pace_min_per_mi")
    if pace:
        for tok in grounding.numeric_tokens(str(pace)):
            v = grounding.parse_number(tok)
            if v is not None:
                pool.append((abs(v), "today_pace_min_per_mi"))

    week_planned_mi = plan_section.get("week_planned_mi")
    if week_planned_mi is not None:
        pool.append((abs(float(week_planned_mi)), "week_planned_mi"))
    week_actual_mi = plan_section.get("week_actual_mi")
    if week_actual_mi is not None:
        pool.append((abs(float(week_actual_mi)), "week_actual_mi"))

    return pool


def ground_coaching_line(text: str, plan_section: dict) -> list[GroundingFlag]:
    """Advisory grounding for the PDF's Claude-generated coaching line — the
    one LLM output entering a user-facing artifact with zero numeric
    validation until now.

    Pure (no I/O). Reuses ``grounding``'s numeric-token parser and
    nearest-match bands (``grounding.numeric_tokens`` / ``parse_number`` /
    ``classify_against_pool``) over a pool built from the deterministic plan
    section (``adherence_pct``, ``days_to_race``, today's
    ``distance_mi``/pace, ``week_planned_mi``/``week_actual_mi`` — see
    ``_plan_section_pool``). Replicates ``flag()``'s empty-pool guard
    explicitly (``if not pool: return []``) — in practice the pool is never
    empty (``adherence_pct`` defaults to 0 in ``_build_plan_section``), but
    the guard is kept since ``_nearest``/``classify_against_pool`` document a
    non-empty-pool precondition.

    Flags always carry ``takeaway_index=0`` — the PDF has exactly one
    coaching line, so there is no takeaway list to index into.

    Never raises on arbitrary text: any parse/lookup failure downgrades to
    "no flags" rather than propagating, matching this module's advisory-
    signal contract (grounding is a measurement, never a gate — see
    ``generate_brief_report``, which only logs these flags).

    Two advisory-signal caveats, so the flags aren't over-read: the pool
    includes string-shaped pace values tokenized the way ``_display_numbers``
    tokenizes a GroundedValue's display string, and a ``days_to_race`` cited
    in prose is typically skipped by ``grounding``'s time-window rule (e.g.
    "12 days to your 10k"). Same partial-coverage character as the brief
    path's ``flag()`` signal.
    """
    try:
        pool = _plan_section_pool(plan_section)
        if not pool:
            return []
        flags: list[GroundingFlag] = []
        for tok in grounding.numeric_tokens(text):
            x = grounding.parse_number(tok)
            if x is None:
                continue
            ax = abs(x)
            verdict, near_val, near_name = grounding.classify_against_pool(ax, pool)
            if verdict == "flag":
                flags.append(GroundingFlag(
                    takeaway_index=0, token=tok.strip(),
                    nearest_metric=near_name, delta=round(x - near_val, 2)))
        return flags
    except Exception:
        # Advisory-only signal, so degrading to "no flags" is right — but
        # silently, it was the one fail-open in the module without a log
        # line (every sibling warns with exc_info).
        _LOG.warning("coaching-line grounding check failed (ignored)",
                     exc_info=True)
        return []
