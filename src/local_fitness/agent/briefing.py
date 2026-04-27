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
from datetime import date, datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
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


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of the agent's response — agents sometimes
    wrap output in a ```json fence even when told not to. Try direct parse,
    then code-fence, then bracket scan. Raises if nothing parses."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Last resort: find first { and matching }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"could not parse JSON from agent response: {e}\n\n{text[:500]}")
    raise ValueError(f"no JSON found in agent response: {text[:500]}")


async def _generate(model: str = DEFAULT_MODEL) -> Brief:
    user_name = db.get_setting("user_name", prompts.DEFAULT_USER_NAME)
    # Default 10k matches the universal benchmark; override via
    # `fitness config set daily_step_goal <N>` if Nate wants a different bar.
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
    )
    chunks: list[str] = []
    async for message in query(
        prompt=prompts.briefing_prompt(user_name, daily_step_goal),
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    raw = "\n".join(chunks).strip()
    payload = _extract_json(raw)
    payload.setdefault("date", date.today().isoformat())
    payload.setdefault("user_name", user_name)
    # Stamp the generation time so the UI can detect when newer data has
    # landed since this brief was written and offer a regenerate banner.
    payload["generated_at"] = datetime.now().isoformat()
    try:
        return Brief.model_validate(payload)
    except ValidationError as e:
        LOG.error("Brief JSON failed validation: %s\n\nRaw: %s", e, raw[:1000])
        raise


def generate_and_save(out_dir: Path | None = None, model: str = DEFAULT_MODEL) -> Path:
    out_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = asyncio.run(_generate(model=model))
    path = out_dir / f"{date.today().isoformat()}.json"
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    LOG.info("Wrote brief to %s", path)
    return path


def load_today(out_dir: Path | None = None) -> Brief | None:
    out_dir = out_dir or DEFAULT_BRIEFINGS_DIR
    path = out_dir / f"{date.today().isoformat()}.json"
    if not path.exists():
        return None
    return Brief.model_validate_json(path.read_text(encoding="utf-8"))
