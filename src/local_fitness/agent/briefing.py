"""Daily morning briefing generator.

Runs the agent and returns a structured Brief (list of Takeaways) so the
UI can render each one as an expandable card with an embedded chart.
Persisted as JSON at ~/localrepo/local-fitness/briefings/YYYY-MM-DD.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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
from . import prompts
from . import tools as agent_tools
from .schemas import Brief

LOG = logging.getLogger(__name__)

def _default_briefings_dir() -> Path:
    """Resolve the briefings directory. Honor LOCAL_FITNESS_BRIEFINGS_DIR
    for container deployments where /briefings is a bind-mounted volume;
    default to the host-CLI path when unset."""
    import os
    override = os.environ.get("LOCAL_FITNESS_BRIEFINGS_DIR")
    if override:
        return Path(override)
    return Path.home() / "localrepo" / "local-fitness" / "briefings"


DEFAULT_BRIEFINGS_DIR = _default_briefings_dir()
DEFAULT_MODEL = "claude-sonnet-4-6"


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"```(?:json)?\s*")


# Models emit JSON with raw newlines inside both string KEYS and VALUES
# (an artefact of streamed token output being formatted with line wraps).
# strict=False alone fixes value-side parsing but the control chars are
# preserved in the resulting Python strings — and when they land in a
# *key* (e.g. ``"headline\n":``), Pydantic can't find the field at all.
# We pre-process the text to remove raw control chars from inside string
# contexts, then parse strict-mode for safety.
_LOOSE_DECODER = json.JSONDecoder(strict=False)


# Valid JSON string escape chars per RFC 8259. Anything else after a
# backslash inside a string is a parse error in strict mode; we strip
# the rogue backslash to keep the literal char intact.
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _strip_inline_control_chars(text: str) -> str:
    """Sanitize JSON strings against the failure modes models routinely
    emit when wrapping output:

    1. **Raw control chars (< 0x20) inside strings.** Strict JSON
       requires these to be escaped as ``\\n`` / ``\\t`` etc.; the model
       sometimes emits literal ones. Stripped.
    2. **Invalid backslash escapes inside strings.** Model writes
       ``\\|`` or ``\\-`` thinking it's escaping markdown (it isn't —
       these aren't JSON escapes). The rogue backslash is dropped so
       the following char survives as a literal.

    Outside string contexts both are valid JSON whitespace / structural
    chars and are preserved.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        # Inside a string from here.
        if ch == "\\":
            if i + 1 < n:
                nxt = text[i + 1]
                if nxt in _VALID_JSON_ESCAPES:
                    # Legit escape — copy both chars verbatim.
                    out.append(ch)
                    out.append(nxt)
                    i += 2
                    continue
                # Invalid escape — drop the backslash, keep the char.
                # If the char is itself a control char, skip it too.
                if ord(nxt) >= 0x20:
                    out.append(nxt)
                i += 2
                continue
            # Trailing backslash with nothing after — just drop it.
            i += 1
            continue
        if ch == '"':
            in_string = False
            out.append(ch)
            i += 1
            continue
        if ord(ch) < 0x20:
            i += 1  # drop raw control char inside the string
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# Numeric literals with stray whitespace (model wrap artefacts) — outside
# string contexts where ``_strip_inline_control_chars`` deliberately
# leaves whitespace alone. Tightens cases like ``1 .1`` → ``1.1`` and
# ``10 112`` → ``10112``. Conservative: only collapses whitespace
# *between* digits or between a digit and ``.`` to avoid mangling legit
# JSON whitespace.
_NUM_GAP_RE = re.compile(r"(?<=\d)\s+(?=[\d.])|(?<=\.)\s+(?=\d)")


def _fix_numeric_gaps_outside_strings(text: str) -> str:
    """Apply ``_NUM_GAP_RE`` only to characters that fall outside string
    contexts (so we never touch user prose with numbers and spaces in it).
    """
    parts: list[str] = []
    buf: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                parts.append("".join(buf))
                buf = []
            continue
        if ch == '"':
            if buf:
                parts.append(_NUM_GAP_RE.sub("", "".join(buf)))
                buf = []
            buf.append(ch)
            in_string = True
            continue
        buf.append(ch)
    if buf:
        if in_string:
            parts.append("".join(buf))
        else:
            parts.append(_NUM_GAP_RE.sub("", "".join(buf)))
    return "".join(parts)


def _salvage_takeaways(payload: dict) -> dict:
    """If the model emitted a deviating top-level shape, try to recover
    the takeaways list before failing.

    Failure mode this exists for: a user note like "show a snapshot
    table at the top" can convince the model to wrap the brief in a
    `{snapshot: ..., takeaways: [...]}` or even bury takeaways inside
    a sibling object. The schema is non-negotiable per the prompt, but
    salvaging is much better than 500'ing the user's regen.

    Returns ``payload`` unchanged when it already has a top-level
    ``takeaways`` list. Otherwise scans nested values for the first
    list-of-dicts that *looks* like a takeaways array (each item has at
    least a ``headline`` key) and returns ``{"takeaways": that_list}``,
    preserving any compatible top-level metadata (date, user_name).
    """
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("takeaways"), list):
        return payload

    def looks_like_takeaways(val: object) -> bool:
        return (
            isinstance(val, list)
            and len(val) > 0
            and all(isinstance(item, dict) and "headline" in item for item in val)
        )

    found: list | None = None

    def walk(node: object) -> None:
        nonlocal found
        if found is not None:
            return
        if looks_like_takeaways(node):
            found = node  # type: ignore[assignment]
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    if found is None:
        return payload  # nothing recognizable; let validation fail downstream
    LOG.warning(
        "Brief output deviated from schema; salvaged %d takeaways from a "
        "nested structure. Tighten prompt or extend schema if this recurs.",
        len(found),
    )
    salvaged: dict = {"takeaways": found}
    for k in ("date", "user_name", "generated_at"):
        v = payload.get(k)
        if isinstance(v, str):
            salvaged[k] = v
    return salvaged


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of the agent's response — agents sometimes
    wrap output in a ```json fence even when told not to. Try direct parse,
    then code-fence, then bracket scan. Raises if nothing parses.

    On a parsed object that lacks a top-level ``takeaways`` field, tries
    to salvage the takeaways from a nested structure before returning —
    a defense against user notes accidentally convincing the model to
    invent new top-level keys.
    """
    cleaned = _fix_numeric_gaps_outside_strings(_strip_inline_control_chars(text.strip()))
    try:
        return _salvage_takeaways(_LOOSE_DECODER.decode(cleaned))
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(cleaned)
    if m:
        try:
            return _salvage_takeaways(_LOOSE_DECODER.decode(m.group(1).strip()))
        except json.JSONDecodeError:
            pass
    # Last resort: find first { and matching }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return _salvage_takeaways(_LOOSE_DECODER.decode(cleaned[start : end + 1]))
        except json.JSONDecodeError as e:
            raise ValueError(f"could not parse JSON from agent response: {e}\n\n{cleaned[:500]}")
    raise ValueError(f"no JSON found in agent response: {cleaned[:500]}")


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
    server = agent_tools.make_server()
    options = ClaudeAgentOptions(
        mcp_servers={agent_tools.SERVER_NAME: server},
        allowed_tools=agent_tools.allowed_tool_names(),
        system_prompt=prompts.system_prompt(user_name),
        model=model,
        permission_mode="bypassPermissions",
        max_turns=20,
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
    try:
        async for message in query(
            prompt=prompts.briefing_prompt(user_name, daily_step_goal),
            options=options,
        ):
            now = time.perf_counter()
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
    try:
        brief = Brief.model_validate(payload)
    except ValidationError as e:
        LOG.error("Brief JSON failed validation: %s\n\nRaw: %s", e, raw[:1000])
        yield {"type": "error", "message": f"Brief failed validation: {e}"}
        return

    if save:
        out_dir = DEFAULT_BRIEFINGS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date.today().isoformat()}.json"
        out_path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
        LOG.info("Wrote brief to %s", out_path)

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


def generate_and_save(out_dir: Path | None = None, model: str = DEFAULT_MODEL) -> Path:
    """CLI / non-streaming entry. The streaming generator already saves to
    DEFAULT_BRIEFINGS_DIR; if a different ``out_dir`` is requested we write
    a second copy there."""
    brief = asyncio.run(_generate(model=model))
    target_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{date.today().isoformat()}.json"
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_today(out_dir: Path | None = None) -> Brief | None:
    out_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    path = out_dir / f"{date.today().isoformat()}.json"
    if not path.exists():
        return None
    return Brief.model_validate_json(path.read_text(encoding="utf-8"))
