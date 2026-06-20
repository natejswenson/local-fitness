"""Claude-FREE brief I/O — the single read/write/salvage gate for briefs.

This module owns ALL non-LLM brief persistence: reading today's brief or the
latest brief on disk, the 7-day continuity summary, the JSON-salvage helpers
that repair the malformed shapes models routinely emit, and — crucially —
``save_brief``, the ONE function that writes ``briefings/YYYY-MM-DD.json``.

It imports only stdlib + ``schemas``/``db``. It MUST NOT import the Agent SDK,
``briefing``, or ``tools`` — keeping it Claude-free and acyclic is what lets the
web server and the MCP server read brief I/O without pulling a Claude loop into
their import graph, and what makes ``save_brief`` a single integrity gate shared
by the scheduled composer, the ``save_brief`` MCP tool, and ``ab_brief --run``
alike.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import db
from . import prompts
from .render import fix_table_row_breaks
from .schemas import Brief

LOG = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _default_briefings_dir() -> Path:
    """Resolve the briefings directory. Honor LOCAL_FITNESS_BRIEFINGS_DIR
    for container deployments where /briefings is a bind-mounted volume;
    default to a project-relative `./briefings/` directory when unset."""
    override = os.environ.get("LOCAL_FITNESS_BRIEFINGS_DIR")
    if override:
        return Path(override)
    return _PROJECT_ROOT / "briefings"


DEFAULT_BRIEFINGS_DIR = _default_briefings_dir()

# Default user name when no `user_name` setting is stored. Kept here (rather
# than only in `prompts`) so `save_brief` can stamp it without importing the
# Claude-bound composer.
DEFAULT_USER_NAME = prompts.DEFAULT_USER_NAME


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


RECENT_BRIEFS_LOOKBACK_DAYS = 7


def _recent_briefs_summary(today: date | None = None, days: int = RECENT_BRIEFS_LOOKBACK_DAYS) -> str:
    """Return a compact rendering of the last ``days`` saved briefs (excluding today).

    Used to give the briefing agent continuity across days — so today's brief
    can reference what it told {user_name} yesterday/last week and call out
    follow-through (or the lack of it). Returns "" when there's no history.
    """
    today = today or date.today()
    if not DEFAULT_BRIEFINGS_DIR.exists():
        return ""
    lines: list[str] = []
    for offset in range(1, days + 1):
        d = today - timedelta(days=offset)
        path = DEFAULT_BRIEFINGS_DIR / f"{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        takeaways = data.get("takeaways") or []
        if not takeaways:
            continue
        lines.append(f"{d.isoformat()}:")
        for tk in takeaways:
            headline = (tk.get("headline") or "").strip()
            tone = (tk.get("tone") or "").strip()
            summary = (tk.get("summary") or "").strip()
            if not headline:
                continue
            lines.append(f"  - [{tone}] {headline}")
            if summary:
                lines.append(f"    {summary}")
    return "\n".join(lines)


def load_today(out_dir: Path | None = None) -> Brief | None:
    """Load TODAY's brief (``briefings/<today>.json``), or None if absent."""
    out_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    path = out_dir / f"{date.today().isoformat()}.json"
    if not path.exists():
        return None
    return Brief.model_validate_json(path.read_text(encoding="utf-8"))


def load_latest(out_dir: Path | None = None) -> Brief | None:
    """Load the most-recent brief on disk (most-recent-by-glob across
    ``briefings/*.json``), skipping unparseable/partial files.

    Filenames are ``YYYY-MM-DD.json`` so a lexical filename sort is
    chronological. Graceful on a missing/empty dir (fresh clone) — returns
    None rather than raising. This is the same pick-most-recent logic
    ``mcp_server._latest_brief_markdown`` hand-rolls; both ``/api/brief``'s
    fallback and the ``fitness://brief/latest`` resource consume it.
    """
    out_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    if not out_dir.exists():
        return None
    candidates = sorted(out_dir.glob("*.json"), key=lambda p: p.name)
    for path in reversed(candidates):
        try:
            return Brief.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # skip unparseable/partial files, try the next most recent
    return None


def save_brief(payload: dict) -> dict:
    """The single validate + atomic-write gate for briefs.

    In order:

    1. **Salvage** the payload — repair the malformed shapes models routinely
       emit. A dict is run through ``_salvage_takeaways`` (recover nested
       takeaways); a raw string (a tool transport may hand JSON text) goes
       through the full ``_extract_json`` path (fence-strip, control-char
       repair, bracket scan).
    2. **Stamp server-side BEFORE validation**, never trusting the payload:
       ``user_name`` via ``setdefault`` (honor a payload value), ``date``
       FORCED to today (keep the on-disk filename and in-document date
       consistent), and ``generated_at`` FORCED to now (it powers the UI
       stale-detection, so the agent can't backdate or omit it).
    3. **Validate** the salvaged, stamped payload against the ``Brief`` schema.
       Raises ``ValidationError`` on failure — callers handle it.
    4. **Atomic write** of ``briefings/<today>.json`` via temp file +
       ``os.replace`` so readers never see a half-written file.
    5. **Return** ``{"saved": True, "date", "path", "brief"}`` where ``brief``
       is the validated ``Brief`` OBJECT — the SAME object validated in step 3
       and written in step 4. In-process callers (``generate_streaming``'s
       ``done`` event, ``generate_and_save``) consume this directly, so the
       streamed brief and the on-disk brief cannot diverge (single validate +
       single write).
    """
    # 1. Salvage.
    if isinstance(payload, str):
        payload = _extract_json(payload)
    else:
        payload = _salvage_takeaways(payload)

    # 1b. Repair collapsed markdown tables in each takeaway's details (the
    #     literal-`n` row-break the brief A/B surfaced at lower effort). Done at
    #     the single write gate so every brief — fan-out, single-call, or
    #     hand-authored — renders cleanly. No-op on details without a table.
    for tk in payload.get("takeaways", []) or []:
        if isinstance(tk, dict) and isinstance(tk.get("details"), str):
            tk["details"] = fix_table_row_breaks(tk["details"])

    # 2. Stamp BEFORE validation. Mirrors briefing.generate_streaming's
    #    setdefault(date) / setdefault(user_name) / forced generated_at, but
    #    additionally FORCES `date` to today so the filename and document date
    #    stay consistent.
    payload.setdefault("user_name", db.get_setting("user_name", DEFAULT_USER_NAME))
    payload["date"] = date.today().isoformat()
    payload["generated_at"] = datetime.now().isoformat()

    # 3. Validate (let it raise on invalid; callers handle).
    brief = Brief.model_validate(payload)

    # 4. Atomic write.
    DEFAULT_BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    final_path = DEFAULT_BRIEFINGS_DIR / f"{brief.date}.json"
    tmp_path = DEFAULT_BRIEFINGS_DIR / f".{brief.date}.json.tmp"
    tmp_path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp_path, final_path)
    LOG.info("Wrote brief to %s", final_path)

    # 5. Return the validated object for in-process callers.
    return {
        "saved": True,
        "date": brief.date,
        "path": str(final_path),
        "brief": brief,
    }
