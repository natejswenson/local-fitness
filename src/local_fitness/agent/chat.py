"""Interactive REPL with the fitness agent and one-shot ask command."""
from __future__ import annotations

import asyncio
import logging
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt

from .. import db
from . import prompts
from . import tools as agent_tools

console = Console()
LOG = logging.getLogger(__name__)


def _options(model: str) -> ClaudeAgentOptions:
    user_name = db.get_setting("user_name", prompts.DEFAULT_USER_NAME)
    server = agent_tools.make_server()
    return ClaudeAgentOptions(
        mcp_servers={agent_tools.SERVER_NAME: server},
        allowed_tools=agent_tools.allowed_tool_names(),
        system_prompt=prompts.system_prompt(user_name),
        model=model,
        permission_mode="bypassPermissions",
        max_turns=50,
    )


async def _chat(model: str) -> None:
    options = _options(model)
    console.print(f"[bold cyan]fitness chat[/] · model: {model} · ctrl-d to exit\n")
    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                user_input = Prompt.ask("[bold green]you[/]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]bye[/]")
                return
            if not user_input.strip():
                continue
            await client.query(user_input)
            buffer: list[str] = []
            # Layer A: per-turn timing. Same key=value rules as briefing —
            # never log block.text or tool result bodies, only names + bytes.
            t0 = time.perf_counter()
            t_first_msg: float | None = None
            t_prev = t0
            tool_count = 0
            tool_duration_sum_ms = 0.0
            pending_tool_names: dict[str, str] = {}
            async for msg in client.receive_response():
                now = time.perf_counter()
                if t_first_msg is None:
                    t_first_msg = now
                    LOG.info(
                        "chat_timing phase=first_message ttfm_ms=%.1f",
                        (t_first_msg - t0) * 1000,
                    )
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            buffer.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_count += 1
                            pending_tool_names[block.id] = block.name
                            delta_ms = (now - t_prev) * 1000
                            LOG.info(
                                "chat_timing phase=tool_use name=%s duration_ms_since_prev=%.1f result_bytes=0",
                                block.name,
                                delta_ms,
                            )
                elif isinstance(msg, UserMessage):
                    content = msg.content
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
                                    "chat_timing phase=tool_result name=%s duration_ms_since_prev=%.1f result_bytes=%d",
                                    name,
                                    delta_ms,
                                    result_bytes,
                                )
                t_prev = now
            t_done = time.perf_counter()
            total_ms = (t_done - t0) * 1000
            ttfm_ms = ((t_first_msg or t_done) - t0) * 1000
            LOG.info(
                "chat_timing phase=summary total_ms=%.1f ttfm_ms=%.1f tool_count=%d "
                "tool_duration_sum_ms=%.1f model=%s",
                total_ms,
                ttfm_ms,
                tool_count,
                tool_duration_sum_ms,
                model,
            )
            console.print()
            console.print(Markdown("\n".join(buffer)))
            console.print()


def run(model: str) -> None:
    asyncio.run(_chat(model))


async def _ask_once(question: str, model: str) -> str:
    options = _options(model)
    chunks: list[str] = []
    t0 = time.perf_counter()
    t_first_msg: float | None = None
    t_prev = t0
    tool_count = 0
    tool_duration_sum_ms = 0.0
    pending_tool_names: dict[str, str] = {}
    async for msg in query(prompt=question, options=options):
        now = time.perf_counter()
        if t_first_msg is None:
            t_first_msg = now
            LOG.info(
                "chat_timing phase=first_message ttfm_ms=%.1f",
                (t_first_msg - t0) * 1000,
            )
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_count += 1
                    pending_tool_names[block.id] = block.name
                    delta_ms = (now - t_prev) * 1000
                    LOG.info(
                        "chat_timing phase=tool_use name=%s duration_ms_since_prev=%.1f result_bytes=0",
                        block.name,
                        delta_ms,
                    )
        elif isinstance(msg, UserMessage):
            content = msg.content
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
                            "chat_timing phase=tool_result name=%s duration_ms_since_prev=%.1f result_bytes=%d",
                            name,
                            delta_ms,
                            result_bytes,
                        )
        t_prev = now
    t_done = time.perf_counter()
    total_ms = (t_done - t0) * 1000
    ttfm_ms = ((t_first_msg or t_done) - t0) * 1000
    LOG.info(
        "chat_timing phase=summary total_ms=%.1f ttfm_ms=%.1f tool_count=%d "
        "tool_duration_sum_ms=%.1f model=%s",
        total_ms,
        ttfm_ms,
        tool_count,
        tool_duration_sum_ms,
        model,
    )
    return "\n".join(chunks).strip()


def ask(question: str, model: str) -> None:
    answer = asyncio.run(_ask_once(question, model))
    console.print(Markdown(answer))
