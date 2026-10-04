"""Toolless Codex CLI transport for briefs and single-shot coaching."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def _output_schema() -> dict:
    """Strict schema accepted by ``codex exec --output-schema``."""
    metric_names = [
        "rhr", "sleep_seconds", "sleep_score", "avg_stress",
        "body_battery_max", "body_battery_min", "vo2_max", "steps",
        "intensity_minutes_moderate", "intensity_minutes_vigorous",
        "ctl", "atl", "tsb",
    ]
    metric = {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": metric_names},
                    "days": {"type": "integer", "minimum": 7, "maximum": 730},
                },
                "required": ["metric", "days"],
                "additionalProperties": False,
            },
            {"type": "null"},
        ]
    }
    takeaway = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "tone": {
                "type": "string",
                "enum": ["positive", "caution", "critical", "neutral"],
            },
            "metric": metric,
            "details": {"type": "string"},
        },
        "required": ["headline", "summary", "tone", "metric", "details"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "takeaways": {
                "type": "array", "items": takeaway, "minItems": 3, "maxItems": 5,
            }
        },
        "required": ["takeaways"],
        "additionalProperties": False,
    }


def _run_codex(
    system_prompt: str,
    user_prompt: str,
    *,
    output_schema: dict,
    model: str | None = None,
    timeout: float = 300.0,
) -> str:
    """Run a tool-isolated, noninteractive Codex composition and return JSON."""
    message = (
        f"{system_prompt}\n\n{user_prompt}\n\n"
        "Do not inspect files, run commands, browse, or use tools. Compose only "
        "from the supplied data and return the requested JSON."
    )
    with tempfile.TemporaryDirectory(prefix="local-fitness-codex-") as tmp:
        tmp_path = Path(tmp)
        schema_path = tmp_path / "brief-schema.json"
        output_path = tmp_path / "brief.json"
        schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
        codex_bin = os.environ.get("LOCAL_FITNESS_CODEX_BIN", "codex").strip() or "codex"
        argv = [
            codex_bin, "exec", "--ephemeral", "--ignore-user-config",
            "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
        ]
        if model:
            argv.extend(["--model", model])
        argv.append("-")
        env = os.environ.copy()
        # These invocations are ephemeral. Keep their SQLite/WAL files local
        # to this process instead of sharing a host-mounted credential store.
        # CODEX_HOME remains intact so refreshed login credentials persist.
        state_path = tmp_path / "state"
        state_path.mkdir(mode=0o700)
        env["CODEX_SQLITE_HOME"] = str(state_path)
        try:
            result = subprocess.run(
                argv, input=message, capture_output=True, text=True,
                timeout=timeout, cwd=tmp_path, env=env,
            )
        except FileNotFoundError as e:
            raise RuntimeError("Codex CLI not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"codex exec timed out after {timeout}s") from e

        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            tail = detail[-1] if detail else "no error detail"
            raise RuntimeError(
                f"codex exec failed (exit {result.returncode}): {tail[:300]}"
            )
        if not output_path.exists() or not output_path.read_text(encoding="utf-8").strip():
            raise RuntimeError("codex exec returned an empty response")
        return output_path.read_text(encoding="utf-8").strip()


def generate_codex_completion(
    system_prompt: str, user_prompt: str, *,
    model: str | None = None, timeout: float = 300.0,
) -> str:
    """Return schema-constrained brief JSON, preserving the existing contract."""
    return _run_codex(system_prompt, user_prompt, output_schema=_output_schema(),
                      model=model, timeout=timeout)


def coaching_provider() -> str:
    """Coaching follows the brief provider unless explicitly configured."""
    provider = os.environ.get(
        "LOCAL_FITNESS_COACH_PROVIDER",
        os.environ.get("LOCAL_FITNESS_BRIEF_PROVIDER", "claude"),
    ).strip().lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("invalid coaching provider; expected claude or codex")
    return provider


def coaching_model(model: str | None = None) -> str | None:
    """Resolve a Codex model independently of each caller's Claude default."""
    return (model or os.environ.get("LOCAL_FITNESS_CODEX_COACH_MODEL")
            or os.environ.get("LOCAL_FITNESS_CODEX_MODEL") or "").strip() or None


def coaching_cache_model(model: str | None = None) -> str:
    """Preserve Claude cache keys; separate Codex and its selected model."""
    if coaching_provider() == "codex":
        return "codex:" + (coaching_model(model) or "cli-default")
    return model or "default"


def generate_codex_text(
    system_prompt: str, user_prompt: str, *,
    model: str | None = None, timeout: float = 90.0,
) -> str:
    """Generate prose inside a strict JSON envelope, then return only prose."""
    schema = {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False,
    }
    raw = _run_codex(
        system_prompt,
        user_prompt + "\nReturn your complete response in the JSON field text.",
        output_schema=schema, model=coaching_model(model), timeout=timeout,
    )
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise RuntimeError("Codex coaching returned invalid JSON") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise RuntimeError("Codex coaching returned invalid text")
    text = payload["text"].strip()
    if not text:
        raise RuntimeError("Codex coaching returned empty text")
    return text
