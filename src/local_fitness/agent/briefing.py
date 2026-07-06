"""Daily morning briefing generator.

Runs the agent and returns a structured Brief (list of Takeaways) so the
UI can render each one as an expandable card with an embedded chart.
Persisted as JSON at ``./briefings/YYYY-MM-DD.json`` (or wherever
``LOCAL_FITNESS_BRIEFINGS_DIR`` points).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import ValidationError

from .. import db
from . import brief_planner
from . import briefs
from . import coach
from . import grounding
from . import local_model
from . import prompts
from . import tools as agent_tools
from .briefs import (
    DEFAULT_BRIEFINGS_DIR,
    _extract_json,
    _recent_briefs_summary,
    _salvage_takeaways,
    _strip_inline_control_chars,
    load_latest,
    load_today,
    save_brief,
)
from .briefs import _FENCE_OPEN_RE, _LOOSE_DECODER
from .render import fix_table_row_breaks
from .schemas import Brief

LOG = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"

# Reasoning effort for the brief composer. Measured 2026-06-20: the brief's
# wall-clock is dominated by extended thinking (~12.7k of ~14k output tokens,
# ~208s of a ~230s brief). A controlled probe showed the SDK's `thinking`
# `budget_tokens` knob is IGNORED on the Claude Code CLI / Max-OAuth path
# (1024 vs 12000 produced the same output), but `effort` and a `disabled`
# thinking config DO propagate — `effort="low"` roughly halved output tokens
# while preserving reasoning. So `effort` is the working speed lever; the
# default `None` behaves like "high". Env-tunable for A/B + container override.
_DEFAULT_BRIEF_EFFORT = "low"
_VALID_EFFORTS = ("low", "medium", "high", "max")


def _brief_effort() -> str:
    """Reasoning effort for the brief composer from the environment.

    ``LOCAL_FITNESS_BRIEF_EFFORT`` ∈ {low, medium, high, max}; unset or
    unrecognized → ``_DEFAULT_BRIEF_EFFORT``. Lower effort = less extended
    thinking = faster brief.
    """
    raw = os.environ.get("LOCAL_FITNESS_BRIEF_EFFORT")
    if raw is None:
        return _DEFAULT_BRIEF_EFFORT
    token = raw.strip().lower()
    return token if token in _VALID_EFFORTS else _DEFAULT_BRIEF_EFFORT


# Agent/code-separation composer. ON by default (cut over 2026-06-27 after
# shadow-run parity held on all 6 fixtures): the deterministic brief_planner
# assembles a BriefContext and a single TOOLLESS generator writes the prose from
# it. The V1 monolith (MCP tools, max_turns=20) is retained as the instant
# rollback — set LOCAL_FITNESS_BRIEF_V2=0 (or false/no/off) to fall back to it.
_BRIEF_V2_ENV = "LOCAL_FITNESS_BRIEF_V2"
_FALSY = frozenset({"0", "false", "no", "off"})


def _brief_v2_enabled() -> bool:
    """V2 unless explicitly disabled. Default (unset) → True; only an explicit
    0/false/no/off rolls back to the V1 tool-driven monolith."""
    return os.environ.get(_BRIEF_V2_ENV, "").strip().lower() not in _FALSY


# Local-model (Ollama) shadow-run support. See
# docs/plans/2026-07-05-gemma4-shadow-run-design.md. A "model" string prefixed
# "ollama:" is dispatched to local_model.generate_local_completion() instead
# of claude_agent_sdk.query() — shadow-run comparison only, never the live
# production path unless a future, separate decision promotes it.
_LOCAL_MODEL_PREFIX = "ollama:"


def _local_model_name(model: str) -> str | None:
    """The Ollama model name if ``model`` is a local-model request, else None."""
    if model.startswith(_LOCAL_MODEL_PREFIX):
        return model[len(_LOCAL_MODEL_PREFIX):]
    return None


def _gemma4_format_schema() -> dict:
    """A tightened variant of Brief's JSON schema for gemma4's structured-
    output constraint.

    Plain ``Brief.model_json_schema()`` fixed schema-COMPLIANCE (12/12 valid,
    0 flakes — see docs/plans/2026-07-05-gemma4-shadow-run-design.md) but the
    model leaned on the schema's stated defaults instead of actively choosing
    values: every ``tone`` came back ``"neutral"`` (its default) and every
    ``metric`` came back ``null`` (its default), even when real, varied data
    was available. Stripping ``tone``'s default and making ``metric``
    required-non-null (removing the null option) forces the grammar-
    constrained decoder to commit to a concrete choice — empirically verified
    to fix the defaulting behavior (varied, contextually-appropriate tones
    and populated metrics on manual spot-checks before this was wired in).

    The prompt also mandates exactly one "workout" and one "steps" takeaway in
    every brief (see prompts.py's Workout/Steps mandate sections) — a
    free-form ``takeaways`` array can't structurally enforce that. A single
    ``contains`` constraint reliably forced one mandated category in testing,
    but combining two via ``allOf`` was NOT reliably honored by Ollama's
    grammar-constrained decoder (one fixture produced zero of either mandated
    category across 3 runs despite the constraint, and still validated as
    schema-conformant — see the design doc's Outcome section). Required
    object keys have been reliable throughout instead (same mechanism as the
    tone/metric fix above), so the mandate is expressed as two required
    slots — ``workout_takeaway`` / ``steps_takeaway`` — plus a free
    ``other_takeaways`` array for the rest (conditioning / HR-recovery /
    wildcard). Verified 9/9 across 3 fixtures before being wired in here.
    ``_reshape_gemma4_slots`` flattens this back into Brief's real
    ``takeaways`` list before validation, so every other consumer of the
    parsed payload only ever sees the one real shape.

    Only affects the schema handed to Ollama for decoding guidance — the
    real ``Brief.model_validate()`` in ``_finalize_brief`` still validates
    against the actual (untightened) application schema, so this can never
    make validation MORE permissive, only guide generation to commit to
    values within the schema's existing constraints.
    """
    schema = Brief.model_json_schema()
    takeaway = schema["$defs"]["Takeaway"]
    takeaway["properties"]["tone"].pop("default", None)
    takeaway["properties"]["metric"] = {
        "$ref": "#/$defs/TakeawayMetric",
        "description": takeaway["properties"]["metric"].get("description", ""),
    }
    takeaway["required"] = ["headline", "summary", "tone", "metric", "details"]

    schema["properties"].pop("takeaways")
    schema["properties"]["workout_takeaway"] = {"$ref": "#/$defs/Takeaway"}
    schema["properties"]["steps_takeaway"] = {"$ref": "#/$defs/Takeaway"}
    schema["properties"]["other_takeaways"] = {
        "type": "array",
        "items": {"$ref": "#/$defs/Takeaway"},
        "minItems": 1,
        "maxItems": 3,
    }
    schema["required"] = [r for r in schema["required"] if r != "takeaways"] + [
        "workout_takeaway", "steps_takeaway", "other_takeaways",
    ]
    return schema


def _reshape_gemma4_slots(raw: str) -> str:
    """Undo ``_gemma4_format_schema``'s explicit-slot shape back into Brief's
    real ``takeaways`` list, so ``_finalize_brief``'s parse/validate path
    stays identical for every model.

    Deliberately does NOT go through ``_extract_json`` — that helper's
    ``_salvage_takeaways`` step would find ``other_takeaways`` (a
    list-of-dicts-with-``headline``) and salvage it AS the takeaways list,
    silently discarding ``workout_takeaway``/``steps_takeaway`` before this
    function ever sees them. Ollama's structured-output mode (``format=``)
    returns strict, unfenced JSON, so a plain decode is sufficient here; a
    genuinely malformed response falls through unchanged and gets the full
    fence-stripping/salvage treatment from ``_extract_json`` inside
    ``_finalize_brief``, reporting the real parse error instead of a
    misleading one from a half-applied salvage.
    """
    try:
        payload = _LOOSE_DECODER.decode(_strip_inline_control_chars(raw.strip()))
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    if "workout_takeaway" not in payload or "steps_takeaway" not in payload:
        return raw
    workout = payload.pop("workout_takeaway")
    steps = payload.pop("steps_takeaway")
    others = payload.pop("other_takeaways", None) or []
    payload["takeaways"] = [workout, steps, *others]
    return json.dumps(payload)


def _format_race_goal_time(seconds: int | None) -> str | None:
    """``6420`` -> ``"1:47:00"``; sub-hour goals drop the leading ``0:``."""
    if seconds is None:
        return None
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _gemma4_plan_prompt_facts(plan_today: dict) -> str:
    """A pre-computed plan-facts block appended to gemma4's user prompt when
    an active training plan is present.

    Manual testing (2026-07-06) showed gemma4 fabricates these facts when
    asked to derive them itself: it reported "14 days" to race when the real
    value was 10, and reported adherence as "on track" when the last graded
    session's verdict was actually "missed" — the OPPOSITE. Stating them
    here reduces (doesn't guarantee — see ``_gemma4_plan_status_appendix``
    for the guaranteed part) the chance the model's own prose contradicts
    the facts. Gemma4-specific: Claude's live path already derives these
    correctly, so this never touches the shared ``brief_v2_user_prompt``.
    """
    goal_type = plan_today.get("goal_type") or "race"
    goal_time = _format_race_goal_time(plan_today.get("target_time_seconds"))
    goal_desc = f"sub-{goal_time} {goal_type}" if goal_time else goal_type
    days = plan_today.get("days_to_race")
    countdown = f"{days} days out" if days is not None else "date not set"
    last_graded = plan_today.get("last_graded")
    adherence = (
        "no session graded yet — do not invent a status" if last_graded is None
        else f'last graded session ("{last_graded.get("description", "?")}") '
             f'verdict: {last_graded.get("verdict", "unknown").upper()}')
    today = plan_today.get("today")
    prescription = (
        "no session scheduled today on the plan" if today is None
        else f'plan prescribes: "{today.get("description", "?")}"')
    return (
        "\n# Plan facts (pre-computed — CITE THESE VERBATIM, do not "
        "recompute days-to-race or re-derive the adherence verdict)\n"
        f"- Goal: {goal_desc}, {countdown}\n"
        f"- Adherence: {adherence}\n"
        f"- Today's prescription: {prescription}\n"
        "Reconcile the prescription against today's recovery signals "
        "(RHR/TSB/sleep) in your OWN words — but the three facts above are "
        "ground truth; never contradict or restate a different number/"
        "verdict.\n"
    )


def _gemma4_plan_status_appendix(plan_today: dict) -> str:
    """A deterministic plan-status sentence appended to the workout
    takeaway's ``details`` AFTER generation — not left to the model.

    Where ``_gemma4_plan_prompt_facts`` only reduces the odds of
    contradiction, this guarantees correctness: computed in Python from the
    same ground-truth plan data Claude already reasons over correctly, so it
    can never invent a countdown or invert a verdict. It also reliably
    matches ``ab_brief._PLAN_KEYWORDS`` (the shadow-run parity gate's
    plan-mention check) since the phrasing is fixed, unlike the model's own
    free-text prose. The model's own headline/summary/details still carry
    the actual coaching judgment (reconciling the prescription against
    today's recovery signals) — only the fact-retrieval step is templated.
    """
    goal_type = plan_today.get("goal_type") or "race"
    goal_time = _format_race_goal_time(plan_today.get("target_time_seconds"))
    goal_desc = f"sub-{goal_time} {goal_type}" if goal_time else goal_type
    days = plan_today.get("days_to_race")
    countdown = (f"{days} days to race day" if days is not None
                 else "race day date not yet set")
    last_graded = plan_today.get("last_graded")
    adherence = (
        "no session graded yet on the plan" if last_graded is None
        else f'adherence on the last graded session '
             f'("{last_graded.get("description", "?")}") was '
             f'{last_graded.get("verdict", "unknown").upper()}')
    today = plan_today.get("today")
    prescription = (
        "the plan calls for no session today" if today is None
        else f'the plan calls for: "{today.get("description", "?")}"')
    return (
        f"\n\n**Training plan status:** {goal_desc}, {countdown}. "
        f"{adherence[0].upper()}{adherence[1:]}. "
        f"Today's session — {prescription}."
    )


def _append_gemma4_plan_status(raw: str, plan_today: dict | None) -> str:
    """Append ``_gemma4_plan_status_appendix`` to the workout takeaway's
    ``details`` — always ``takeaways[0]`` post-``_reshape_gemma4_slots``.

    A no-op when there's no active plan, or if ``raw`` doesn't parse or has
    no takeaways — those fall through to the normal parse-failure path.
    """
    if plan_today is None:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    takeaways = payload.get("takeaways")
    if not isinstance(takeaways, list) or not takeaways:
        return raw
    workout = takeaways[0]
    if not isinstance(workout, dict):
        return raw
    workout["details"] = (workout.get("details") or "") + _gemma4_plan_status_appendix(plan_today)
    return json.dumps(payload)


def _assert_fixture_only_data() -> None:
    """Refuse to call a local model unless the DB *and* recent-briefs data are
    fixture-only (both resolve under the system temp dir).

    Local models run outside Claude's data-handling boundary, so this must be
    an ALLOW-list check — it positively confirms fixture isolation rather than
    merely failing to match one hardcoded "real" path. A deny-list check
    against the dev-host default DB path would silently pass under a container
    deployment (LOCAL_FITNESS_DATA_DIR=/data) where the real path differs from
    that literal — see the design doc's round-2 findings.
    """
    tmp_root = Path(tempfile.gettempdir()).resolve()
    for label, path in (
        ("db.DEFAULT_DB_PATH", db.DEFAULT_DB_PATH),
        ("briefs.DEFAULT_BRIEFINGS_DIR", briefs.DEFAULT_BRIEFINGS_DIR),
    ):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(tmp_root):
            raise RuntimeError(
                f"refusing local-model call: {label}={resolved} is not under "
                f"the system temp dir ({tmp_root}) — local models must only "
                "ever see fixture data, never real personal data")


# Back-compat re-exports: existing callers import these from `briefing`
# (server.py, mcp_server.py, ab_brief.py). They now live in `briefs.py`; the
# composer persists THROUGH `briefs.save_brief` and reads via these helpers.
# Keep the names importable here so those callers don't break (later waves
# repoint them directly at `briefs`).
__all__ = [
    "DEFAULT_BRIEFINGS_DIR",
    "DEFAULT_MODEL",
    "load_today",
    "load_latest",
    "save_brief",
    "_recent_briefs_summary",
    "_salvage_takeaways",
    "_extract_json",
    "_strip_inline_control_chars",
    "generate_streaming",
    "generate_and_save",
]


def _iter_partial_takeaways(text: str, skip_count: int):
    """Yield complete takeaway dicts from the model's accumulating text.

    Uses ``json.JSONDecoder.raw_decode`` to parse one object at a time from
    inside the ``"takeaways": [ ... ]`` array. Robust to partial input — when
    raw_decode raises, we stop and wait for more text. ``skip_count`` skips
    items already yielded on prior calls.
    """
    # Strip the opening code fence if present. Don't require a closing fence
    # (it won't exist mid-stream).
    fence = _FENCE_OPEN_RE.search(text)
    if fence:
        text = text[fence.end():]
    # Drop raw control chars inside string contexts so keys like
    # ``"headline\n"`` don't leak through to Pydantic.
    text = _strip_inline_control_chars(text)
    idx = text.find('"takeaways"')
    if idx < 0:
        return
    arr_start = text.find("[", idx)
    if arr_start < 0:
        return
    pos = arr_start + 1
    found = 0
    n = len(text)
    while pos < n:
        # Skip whitespace and commas between objects.
        while pos < n and text[pos] in " \n\r\t,":
            pos += 1
        if pos >= n or text[pos] == "]":
            return
        try:
            obj, end = _LOOSE_DECODER.raw_decode(text, pos)
        except json.JSONDecodeError:
            return  # incomplete object — wait for more text
        found += 1
        if found > skip_count and isinstance(obj, dict):
            yield obj
        pos = end


async def _finalize_brief(raw: str, user_name: str, save: bool, brief_context):
    """Shared tail: parse -> validate -> (optionally save) -> ground -> yield.

    Used by both the Claude streaming path and the local-model (non-streaming)
    path in ``generate_streaming`` so parsing/validation/grounding logic lives
    in exactly one place — any quality difference between models is
    attributable to the model, not to divergently-implemented post-processing.
    """
    try:
        payload = _extract_json(raw)
    except ValueError as e:
        LOG.error("Brief JSON parse failed: %s", e)
        yield {"type": "error", "message": f"Could not parse brief JSON: {e}"}
        return
    payload.setdefault("date", date.today().isoformat())
    payload.setdefault("user_name", user_name)
    payload["generated_at"] = datetime.now().isoformat()
    # Repair collapsed markdown tables in the common path so BOTH the save path
    # (save_brief repairs again — idempotent) and the eval/save=False path emit
    # clean tables. See agent/render.fix_table_row_breaks.
    for _tk in payload.get("takeaways", []) or []:
        if isinstance(_tk, dict) and isinstance(_tk.get("details"), str):
            _tk["details"] = fix_table_row_breaks(_tk["details"])

    if save:
        # Persist through the single write gate. `save_brief` re-stamps,
        # validates ONCE, and returns the validated Brief — we emit THAT object
        # so the on-disk and streamed briefs are identical (no parallel
        # in-composer validate on the save path).
        try:
            result = save_brief(payload)
        except ValidationError as e:
            LOG.error("Brief JSON failed validation: %s\n\nRaw: %s", e, raw[:1000])
            yield {"type": "error", "message": f"Brief failed validation: {e}"}
            return
        if brief_context is not None:
            grounding.log_grounding(result["brief"], brief_context)
        yield {"type": "done", "brief": result["brief"].model_dump()}
        return

    # save=False (eval/scoring): validate locally to produce the done Brief
    # without persisting.
    try:
        brief = Brief.model_validate(payload)
    except ValidationError as e:
        LOG.error("Brief JSON failed validation: %s\n\nRaw: %s", e, raw[:1000])
        yield {"type": "error", "message": f"Brief failed validation: {e}"}
        return
    if brief_context is not None:
        grounding.log_grounding(brief, brief_context)
    yield {"type": "done", "brief": brief.model_dump()}


async def generate_streaming(model: str = DEFAULT_MODEL, save: bool = True):
    """Run the briefing agent and yield NDJSON-shaped events as the model emits.

    Yields one of:
      ``{"type": "takeaway", "index": N, "takeaway": {...}}`` per parsed item
      ``{"type": "done", "brief": {...}}`` once the full brief validates + saves
      ``{"type": "error", "message": "..."}`` on validation/parse failure

    When ``save=True`` (the production path) the brief is written to
    ``DEFAULT_BRIEFINGS_DIR`` before ``done`` is yielded so the cached GET
    ``/api/brief`` returns the new brief immediately. Set ``save=False`` for
    evaluation/scoring callers that don't want to clobber the live brief.
    """
    user_name = db.get_setting("user_name", prompts.DEFAULT_USER_NAME)
    try:
        daily_step_goal = int(db.get_setting("daily_step_goal", "10000") or "10000")
    except ValueError:
        daily_step_goal = 10000
    coach_profile = coach.resolve_coach_profile()
    recent_briefs = _recent_briefs_summary()
    # The V2 BriefContext, kept for the post-stream advisory grounding check.
    # None on the V1 path (no toolless context → nothing to ground against).
    brief_context = None

    local_model_name = _local_model_name(model)
    if local_model_name is not None:
        # Shadow-run-only path (see docs/plans/2026-07-05-gemma4-shadow-run-
        # design.md): local models are toolless-V2-only, never the tool-calling
        # V1 monolith, so a local model can never be given MCP tool access.
        if not _brief_v2_enabled():
            raise ValueError(
                'local models ("ollama:...") are only supported on the V2 '
                "toolless path — refusing to give one MCP tool access")
        brief_context = brief_planner.assemble_brief_context(today=date.today().isoformat())
        # Round 2 finding (2026-07-05): a gemma4-specific prompt appendix
        # (prompts.brief_v2_system_prompt_gemma4/user_prompt_gemma4) did NOT
        # improve schema compliance, and structured output alone fixed
        # compliance but revealed the model leaning on schema DEFAULTS
        # (tone="neutral", metric=null) instead of actively choosing values —
        # redundant prose instructions compete with content reasoning rather
        # than helping it. So: base (shared, Claude-identical) prompt for
        # narrative content, and for gemma4 specifically, a TIGHTENED format
        # schema (no tone default, metric required-non-null) plus a higher
        # temperature (fabrication was never the risk, so 0.4 was overly
        # conservative) — this fixed the defaulting behavior empirically.
        local_system_prompt = prompts.brief_v2_system_prompt(user_name, coach_profile)
        local_prompt_text = prompts.brief_v2_user_prompt(
            brief_context, user_name, daily_step_goal, recent_briefs, coach_profile)
        if local_model_name == "gemma4" and brief_context.plan_today is not None:
            local_prompt_text += _gemma4_plan_prompt_facts(brief_context.plan_today)
        _assert_fixture_only_data()
        if local_model_name == "gemma4":
            local_format = _gemma4_format_schema()
            local_temperature = 0.8
        else:
            local_format = Brief.model_json_schema()
            local_temperature = 0.4
        raw = (await asyncio.to_thread(
            local_model.generate_local_completion,
            local_system_prompt, local_prompt_text, model=local_model_name,
            format=local_format, temperature=local_temperature,
        )).strip()
        if local_model_name == "gemma4":
            raw = _reshape_gemma4_slots(raw)
            raw = _append_gemma4_plan_status(raw, brief_context.plan_today)
        async for evt in _finalize_brief(raw, user_name, save, brief_context):
            yield evt
        return

    if _brief_v2_enabled():
        # V2 (agent/code separation): the deterministic planner gathers the data,
        # evaluates triggers, and ranks candidates; ONE toolless generator
        # (no MCP server attached, max_turns=1) writes the prose from the typed
        # BriefContext. Toolless is what makes grounding sound — the model cannot
        # obtain a number outside the context it was handed.
        brief_context = brief_planner.assemble_brief_context(today=date.today().isoformat())
        options = ClaudeAgentOptions(
            system_prompt=prompts.brief_v2_system_prompt(user_name, coach_profile),
            model=model,
            permission_mode="bypassPermissions",
            max_turns=1,
            effort=_brief_effort(),
            include_partial_messages=True,
        )
        prompt_text = prompts.brief_v2_user_prompt(
            brief_context, user_name, daily_step_goal, recent_briefs, coach_profile)
    else:
        server = agent_tools.make_server()
        options = ClaudeAgentOptions(
            mcp_servers={agent_tools.SERVER_NAME: server},
            # Brief generation is restricted to read-only tools: it must never be
            # able to mutate data (log workouts/observations, delete notes), and
            # excluding daily_snapshot/list_observations keeps the brief's tool set
            # — and therefore its behavior — unchanged. Chat + the web agent keep
            # the full set via allowed_tool_names().
            allowed_tools=agent_tools.read_only_tool_names(),
            system_prompt=prompts.system_prompt(user_name, coach_profile),
            model=model,
            permission_mode="bypassPermissions",
            max_turns=20,
            # Reasoning effort is the working lever on the measured dominant cost
            # (extended thinking). See _brief_effort() / LOCAL_FITNESS_BRIEF_EFFORT.
            effort=_brief_effort(),
            # Required for true mid-token streaming. Without this the SDK only
            # delivers AssistantMessage events at end-of-turn — meaning the
            # entire JSON brief lands in a single TextBlock at the end and our
            # partial-takeaway parser has nothing to chew on until the model
            # is already finished. With it on we receive StreamEvent records
            # carrying the raw Anthropic content_block_delta events, so each
            # token chunk is visible in real time.
            include_partial_messages=True,
        )
        prompt_text = prompts.briefing_prompt(
            user_name, daily_step_goal, recent_briefs, coach_profile)
    chunks: list[str] = []
    # NDJSON state — yield each takeaway exactly once as it appears in the
    # accumulating model output.
    yielded_takeaways = 0
    # Layer A timing instrumentation. We log key=value pairs (no PHI — only
    # tool names, byte counts, and durations) so we can grep + awk later
    # without parsing JSON. NEVER log block.text or tool result content.
    t0 = time.perf_counter()
    t_first_msg: float | None = None
    t_first_card: float | None = None
    t_prev = t0
    tool_count = 0
    tool_duration_sum_ms = 0.0
    pending_tool_names: dict[str, str] = {}
    loop_exit_reason = "normal"
    # Token-usage capture (Phase 0 latency attribution). The end-of-turn
    # ResultMessage carries a usage payload; we keep the last one seen so the
    # summary log can report output-token volume — the signal that tells us
    # whether the brief's wall-clock is thinking/generation (high output
    # tokens) vs. serial tool round-trips (many tool_use turns, modest output).
    last_usage: dict | None = None
    if recent_briefs:
        # Count date headers (lines ending in ":" with no leading whitespace) —
        # one per past brief included.
        days_present = sum(
            1 for ln in recent_briefs.split("\n")
            if ln and not ln.startswith(" ") and ln.endswith(":")
        )
        LOG.info(
            "brief_recent_history days_present=%d chars=%d",
            days_present,
            len(recent_briefs),
        )
    try:
        async for message in query(
            prompt=prompt_text,
            options=options,
        ):
            now = time.perf_counter()
            _u = getattr(message, "usage", None)
            if _u is not None:
                last_usage = dict(_u) if isinstance(_u, dict) else getattr(_u, "__dict__", None)
            if t_first_msg is None:
                t_first_msg = now
                LOG.info(
                    "brief_timing phase=first_message ttfm_ms=%.1f",
                    (t_first_msg - t0) * 1000,
                )
            if isinstance(message, StreamEvent):
                # Partial-message stream events carry the raw Anthropic API
                # event payload. The text-delta event is the only one we need
                # for live streaming. AssistantMessage will still arrive at
                # end-of-turn with the same TextBlock — we ignore its text
                # there to avoid double-counting.
                ev = message.event or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            chunks.append(text)
                            accumulated = "".join(chunks)
                            for tk in _iter_partial_takeaways(accumulated, yielded_takeaways):
                                if t_first_card is None:
                                    t_first_card = time.perf_counter()
                                    LOG.info(
                                        "brief_timing phase=first_card ms_from_start=%.1f",
                                        (t_first_card - t0) * 1000,
                                    )
                                yield {
                                    "type": "takeaway",
                                    "index": yielded_takeaways,
                                    "takeaway": tk,
                                }
                                yielded_takeaways += 1
            elif isinstance(message, AssistantMessage):
                # Tool-use blocks still arrive as full AssistantMessage events
                # at end-of-turn — keep timing instrumentation here. Skip the
                # TextBlocks (we already streamed them via StreamEvent above).
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_count += 1
                        pending_tool_names[block.id] = block.name
                        delta_ms = (now - t_prev) * 1000
                        LOG.info(
                            "brief_timing phase=tool_use name=%s duration_ms_since_prev=%.1f result_bytes=0",
                            block.name,
                            delta_ms,
                        )
            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            delta_ms = (now - t_prev) * 1000
                            tool_duration_sum_ms += delta_ms
                            result_bytes = 0
                            c = block.content
                            if isinstance(c, str):
                                result_bytes = len(c)
                            elif isinstance(c, list):
                                for item in c:
                                    if isinstance(item, dict):
                                        txt = item.get("text")
                                        if isinstance(txt, str):
                                            result_bytes += len(txt)
                            name = pending_tool_names.pop(block.tool_use_id, "unknown")
                            LOG.info(
                                "brief_timing phase=tool_result name=%s duration_ms_since_prev=%.1f result_bytes=%d",
                                name,
                                delta_ms,
                                result_bytes,
                            )
            t_prev = now
    except asyncio.CancelledError:
        loop_exit_reason = "cancelled"
        # Surface why no brief was saved. CancelledError is a BaseException —
        # the FastAPI endpoint's `except Exception` doesn't catch it, so
        # without this log the regen vanishes silently after a client
        # disconnect or shutdown.
        LOG.warning(
            "brief_stream cancelled mid-flight chars=%d takeaways_yielded=%d tool_count=%d",
            sum(len(c) for c in chunks),
            yielded_takeaways,
            tool_count,
        )
        raise
    except BaseException as e:
        loop_exit_reason = f"exception:{type(e).__name__}"
        LOG.exception(
            "brief_stream errored mid-flight chars=%d takeaways_yielded=%d tool_count=%d",
            sum(len(c) for c in chunks),
            yielded_takeaways,
            tool_count,
        )
        raise
    LOG.info(
        "brief_stream loop_exit reason=%s chars=%d takeaways_yielded=%d tool_count=%d",
        loop_exit_reason,
        sum(len(c) for c in chunks),
        yielded_takeaways,
        tool_count,
    )
    t_done = time.perf_counter()
    total_ms = (t_done - t0) * 1000
    ttfm_ms = ((t_first_msg or t_done) - t0) * 1000
    LOG.info(
        "brief_timing phase=summary total_ms=%.1f ttfm_ms=%.1f tool_count=%d "
        "tool_duration_sum_ms=%.1f model=%s",
        total_ms,
        ttfm_ms,
        tool_count,
        tool_duration_sum_ms,
        model,
    )
    if last_usage is not None:
        LOG.info(
            "brief_usage output_tokens=%s input_tokens=%s "
            "cache_read=%s cache_creation=%s",
            last_usage.get("output_tokens"),
            last_usage.get("input_tokens"),
            last_usage.get("cache_read_input_tokens"),
            last_usage.get("cache_creation_input_tokens"),
        )

    # Final validation + save. If parsing fails after the stream completes,
    # surface the error event so the UI can show a clear message instead of
    # silently leaving the placeholder cards.
    raw = "\n".join(chunks).strip()
    async for evt in _finalize_brief(raw, user_name, save, brief_context):
        yield evt


async def _generate(model: str = DEFAULT_MODEL, save: bool = False) -> Brief:
    """Drain the streaming generator into a complete Brief.

    Eval / read helper (the A/B harness is the only caller; ``/api/brief`` reads
    the saved file, it does not generate). Defaults ``save=False`` so it can
    NEVER overwrite the live ``briefings/<date>.json`` — the production save path
    is ``generate_and_save`` (which uses ``generate_streaming(save=True)``)."""
    last_brief: dict | None = None
    async for evt in generate_streaming(model=model, save=save):
        if evt["type"] == "done":
            last_brief = evt["brief"]
        elif evt["type"] == "error":
            raise ValueError(evt["message"])
    if last_brief is None:
        raise ValueError("Brief generation completed without a done event")
    return Brief.model_validate(last_brief)


def generate_and_save(model: str = DEFAULT_MODEL) -> Path:
    """CLI / non-streaming entry. Runs the composer with ``save=True`` so the
    brief is persisted exactly once, through ``briefs.save_brief`` (inside
    ``generate_streaming``). Returns the path ``save_brief`` wrote so
    ``cli.py``'s "Brief written to: {path}" echo keeps working."""
    last_path: str | None = None
    last_brief: dict | None = None

    async def _run() -> None:
        nonlocal last_brief
        async for evt in generate_streaming(model=model, save=True):
            if evt["type"] == "done":
                last_brief = evt["brief"]
            elif evt["type"] == "error":
                raise ValueError(evt["message"])

    asyncio.run(_run())
    if last_brief is None:
        raise ValueError("Brief generation completed without a done event")
    # The save path wrote briefings/<date>.json; reconstruct the same path the
    # gate produced (date is server-stamped to today inside save_brief).
    last_path = str(DEFAULT_BRIEFINGS_DIR / f"{last_brief['date']}.json")
    return Path(last_path)
