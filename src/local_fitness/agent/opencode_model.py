"""Thin client for calling any opencode-CLI-reachable model.

Complements ``local_model.py`` (Ollama-provider models keep the direct-HTTP,
grammar-constrained-decoding path there) — this module is the transport for
every OTHER provider ``opencode`` can reach (e.g. opencode's own hosted
gateway models like ``opencode/deepseek-v4-flash-free``), invoked via the
``opencode run`` CLI subprocess rather than a direct HTTP client, since
``opencode`` is the only access path to those models. Shadow-run diagnostic
only, driven by ``agent.briefing``'s ``"opencode:"``-prefixed model dispatch —
never the live production brief job. See
docs/plans/2026-07-08-model-agnostic-shadow-run-design.md.
"""
from __future__ import annotations

import json
import logging
import subprocess

LOG = logging.getLogger(__name__)

_AGENT_FALLBACK_MARKER = "not found. Falling back to default agent"


def _check_agent_configured(agent: str, *, timeout: float) -> None:
    """Fail-closed pre-check: verify ``agent`` exists in opencode's own agent
    list before ever sending a prompt.

    Two distinct failure shapes, since they point at different remediations:
    the check subprocess itself failing (opencode broken/unauthenticated/
    unreachable) vs. the check succeeding but the agent simply not being
    configured.
    """
    try:
        result = subprocess.run(
            ["opencode", "agent", "list"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "opencode binary not found on PATH — install the opencode CLI first"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"opencode agent pre-check timed out after {timeout}s — opencode "
            "itself may be unresponsive"
        ) from e

    if result.returncode != 0:
        stderr_line = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
        raise RuntimeError(
            f"opencode itself failed to respond (agent pre-check exit "
            f"{result.returncode}): {stderr_line[:200]}"
        )

    if agent not in result.stdout:
        raise RuntimeError(
            f"see .env.example for opencode agent setup — configured agent "
            f"{agent!r} not found in 'opencode agent list' output"
        )


def generate_opencode_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    agent: str,
    timeout: float = 300.0,
) -> str:
    """Call ``opencode run`` for a non-Ollama model and return the reply text.

    ``model`` is the full ``"provider/model"`` string opencode's own catalog
    uses (e.g. ``"opencode/deepseek-v4-flash-free"``). ``agent`` must name a
    tool-free opencode agent (see ``.env.example``'s
    ``LOCAL_FITNESS_OPENCODE_AGENT`` setup) — this transport sends real
    personal-coaching-derived prompt text to a third-party gateway, so the
    agent must never have bash/edit/tool access.

    Two-tier safety check against opencode's undocumented behavior of
    silently falling back to its tool-enabled ``build`` agent when
    ``--agent`` names an agent that doesn't exist (confirmed by direct
    testing — opencode does not error on this, it warns and falls back):

    1. Fail-closed PRE-CHECK (primary, and effectively the only, guard
       against unsafe on-host tool execution): ``opencode agent list`` runs
       before ``opencode run`` is ever invoked, verifying the agent exists
       rather than inferring failure from opencode's stderr wording.
    2. Output-integrity BACKSTOP (secondary — a different property, not
       execution prevention): scans stderr for opencode's own
       fallback-warning substring. By the time this scan runs, a fallback
       (if any) has already executed — this only stops the caller from
       trusting a response that may have come from the wrong, tool-enabled
       agent.

    Raises ``RuntimeError`` on any failure (opencode binary missing, the
    pre-check subprocess itself failing, the configured agent missing,
    non-zero exit, timeout, empty/malformed NDJSON output, or a detected
    agent-fallback) — one normalized shape, mirroring ``local_model.py``.
    """
    _check_agent_configured(agent, timeout=timeout)

    message = f"{system_prompt}\n\n{user_prompt}"
    # Unconditional at this call site, by construction: this function is
    # only ever reached for provider != "ollama" (Ollama-provider models are
    # routed to local_model.py instead), so every send that gets here is
    # off-machine. Placed immediately before the `opencode run` subprocess
    # call, AFTER the fail-closed pre-check above has already passed — never
    # at function entry, never before the pre-check (which would emit a log
    # even on the pre-check-failure path).
    LOG.warning("opencode transport: sending prompt to off-machine provider %s", model)
    try:
        result = subprocess.run(
            ["opencode", "run", "-m", model, "--format", "json", "--agent", agent,
             "--", message],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "opencode binary not found on PATH — install the opencode CLI first"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"opencode run timed out after {timeout}s") from e

    if _AGENT_FALLBACK_MARKER in result.stderr:
        raise RuntimeError(
            f"see .env.example for opencode agent setup — opencode fell back "
            f"to its default tool-enabled agent (configured agent {agent!r} "
            "not recognized)"
        )

    if result.returncode != 0:
        stderr_line = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
        raise RuntimeError(f"opencode run failed (exit {result.returncode}): {stderr_line[:200]}")

    text_parts: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"opencode call failed (malformed NDJSON line): {e}") from e
        if event.get("type") == "text":
            part = event.get("part", {})
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                text_parts.append(text)

    if not text_parts:
        raise RuntimeError("opencode call failed: empty response (no text content in output)")
    return "".join(text_parts)
