"""Toolless Codex CLI transport for the production V2 brief composer."""
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


def generate_codex_completion(
    system_prompt: str,
    user_prompt: str,
    *,
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
        schema_path.write_text(json.dumps(_output_schema()), encoding="utf-8")
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
