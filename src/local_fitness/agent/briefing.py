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
from . import coach
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
    recent_briefs = _recent_briefs_summary()
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
            prompt=prompts.briefing_prompt(user_name, daily_step_goal, recent_briefs, coach_profile),
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
    yield {"type": "done", "brief": brief.model_dump()}


async def _generate(model: str = DEFAULT_MODEL) -> Brief:
    """Drain the streaming generator into a complete Brief. Used by the
    non-streaming endpoint and the CLI brief command."""
    last_brief: dict | None = None
    async for evt in generate_streaming(model=model):
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
