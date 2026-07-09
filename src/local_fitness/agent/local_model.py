"""Thin client for calling a local model via Ollama's HTTP API.

Used by ``agent.briefing``'s ``"opencode:"``-prefixed alt-model dispatch
(``_alt_model_name()``) for the ``ollama`` provider specifically — every
other provider routes through ``opencode_model.py``'s opencode-CLI transport
instead, since this module talks directly to a local Ollama daemon
(``http://localhost:11434``) and has no equivalent for off-machine models.
Runs the V2 toolless brief generator against a local model (e.g. gemma4)
instead of Claude, for shadow-run comparison. Stdlib-only
(``urllib.request``) since neither ``httpx`` nor ``ollama`` is currently a
project dependency and this is a diagnostic path, not (yet) a production
one. See docs/plans/2026-07-08-model-agnostic-shadow-run-design.md
(generalizes docs/plans/2026-07-05-gemma4-shadow-run-design.md's original
gemma4/Ollama-only path).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "gemma4"


def generate_local_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    think: bool = False,
    temperature: float = 0.4,
    timeout: float = 300.0,
    format: dict | str | None = None,  # noqa: A002 - matches Ollama's own field name
) -> str:
    """Call Ollama's ``/api/chat`` (non-streaming) and return the reply text.

    ``think=False`` disables gemma4's extended-reasoning mode — narrating an
    already-grounded context doesn't need it, and it mirrors Claude's
    ``effort="low"`` speed lever on the existing path (a different mechanism,
    not an equivalent one). Temperature is lowered from gemma4's default
    (1.0) to bias toward faithful narration over creative flourish.

    ``format`` is Ollama's structured-output constraint: pass a JSON Schema
    dict (e.g. ``Brief.model_json_schema()``) to force grammar-constrained,
    schema-conformant output, or ``"json"`` for loose JSON mode. Verified
    (2026-07-05 shadow-run round 2) to fix the schema-compliance failures a
    stricter prompt alone couldn't — capitalized enum values, bare-string
    `metric`, missing required fields all disappeared with a real schema.
    Worth watching: it also seemed to bias the model toward schema DEFAULT
    values (tone="neutral", metric=null) rather than actively choosing them —
    a still-valid but potentially less useful output, not caught by schema
    validation alone.

    Raises ``RuntimeError`` on any failure (connection refused, non-2xx,
    malformed body) — normalized to one shape so callers (e.g. the shadow-run
    scripts' flake recording) get a consistent, readable reason instead of
    three different low-level exception types.
    """
    payload_body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": think,
        "options": {"temperature": temperature},
    }
    if format is not None:
        payload_body["format"] = format
    body = json.dumps(payload_body).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"ollama call failed (connection): {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ollama call failed (malformed response body): {e}") from e
    try:
        content = payload["message"]["content"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(
            f"ollama call failed (unexpected response shape): {payload!r}") from e
    if not isinstance(content, str):
        raise RuntimeError(
            f"ollama call failed (unexpected response shape): {payload!r}")
    return content
