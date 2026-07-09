"""Claude-generated coaching line for the PDF report's Training Plan section.

Deliberately its own module rather than folded into ``briefing.py`` (owns
the whole daily-brief generation lifecycle, eval'd against
``baseline.json``/fixtures — a different concern and lifecycle) or
``visuals.py`` (pure rendering, no LLM calls, no DB access — stays that
way). The Claude Agent SDK is imported inside ``generate_coaching_line``'s
body, never at module scope: ``agent/tools.py`` (which imports this module)
is imported by the always-running containerized web server, and that
process must never pay the SDK's import cost for a PDF-only feature it
never uses — mirrors ``visuals.py``'s deferred matplotlib/weasyprint
imports for the same reason.

``briefing`` is ALSO imported lazily (inside ``generate_coaching_line``,
not at module scope) for a second, load-bearing reason, not just import
cost: ``briefing.py`` itself imports ``tools.py`` at module scope (as
``agent_tools``, for the V1 monolith's MCP-tool wiring), and ``tools.py``
imports this module — a module-scope ``from . import briefing`` here
would close that into a real circular import (``tools -> plan_coach ->
briefing -> tools``) that breaks at process start. Deferring to call time
sidesteps it entirely, since by then every module involved has already
finished initializing.
"""
from __future__ import annotations

import asyncio

from .coach import CoachProfile

_VERDICT_PHRASE = {
    "done": "You hit yesterday's session clean.",
    "partial": "Yesterday came up short of the prescription.",
    "missed": "Yesterday was a skip.",
    "compliant": "Yesterday was a scheduled rest day.",
}


def _format_prescription(today_workout: dict) -> str:
    prescription = today_workout["type"]
    if today_workout.get("distance_mi") is not None:
        prescription += f" {today_workout['distance_mi']} mi"
    if today_workout.get("pace_min_per_mi"):
        prescription += f" @ {today_workout['pace_min_per_mi']}/mi"
    return prescription


def build_prompt(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
) -> tuple[str, str]:
    """Assemble the ``(system_prompt, user_prompt)`` pair for the coaching
    line. Pure string assembly — no I/O, no randomness, fully unit-testable.
    """
    system_prompt = (
        "You are Nate's running coach, writing ONE short paragraph (2-4 "
        "sentences, no more) that preps him for today's prescribed run.\n\n"
        f"{profile.dials_line}\n\n{profile.persona}\n\n"
        "Output ONLY the coaching paragraph itself — no headline, no "
        'markdown, no quotation marks, no preamble like "Here\'s your line".'
    )

    lines = [f"Today's prescribed workout: {_format_prescription(today_workout)}."]
    if today_workout.get("description"):
        lines.append(f"Prescription notes: {today_workout['description']}")

    lines.append(f"Plan adherence over the last graded stretch: {adherence_pct}%.")
    if days_to_race is not None:
        lines.append(f"{days_to_race} days to the {goal_type}.")
    else:
        lines.append(f"Goal: {goal_type}.")

    if last_7_days:
        lines.append("Last 7 days, most recent first:")
        for day in last_7_days:
            planned = f"{day['planned_mi']} mi" if day.get("planned_mi") is not None else "—"
            actual = f"{day['actual_mi']} mi" if day.get("actual_mi") is not None else "—"
            lines.append(
                f"  {day['date']}: {day['type']} — planned {planned}, "
                f"actual {actual}, verdict {day['verdict']}"
            )

    return system_prompt, "\n".join(lines)


async def generate_coaching_line(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
    *,
    model: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Claude-generated coaching line prepping Nate for today's run.

    Raises on any failure (missing/expired credential, network, timeout,
    empty response) — the caller (``tools.generate_brief_report``) is
    responsible for falling back to ``fallback_coaching_line``. ``model``
    (``None`` by default) resolves to ``briefing.DEFAULT_MODEL`` — the same
    constant the real daily brief generator always uses — read at call
    time (not as a function-signature default) to avoid a module-scope
    import of ``briefing`` here; see the module docstring for why. This
    call follows ``DEFAULT_MODEL`` automatically if that constant ever
    changes, rather than duplicating a literal that could drift out of
    sync.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    from . import briefing

    if model is None:
        model = briefing.DEFAULT_MODEL

    system_prompt, user_prompt = build_prompt(
        profile, today_workout, last_7_days, adherence_pct, days_to_race, goal_type,
    )
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=1,
    )

    async def _run() -> str:
        chunks: list[str] = []
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks).strip()

    text = await asyncio.wait_for(_run(), timeout=timeout)
    if not text:
        raise RuntimeError("coaching-line generator returned an empty response")
    return text


def fallback_coaching_line(
    today_workout: dict,
    last_7_days: list[dict],
    days_to_race: int | None,
    goal_type: str,
) -> str:
    """Deterministic, template-based coaching line — used only when
    ``generate_coaching_line`` fails. Pure: identical inputs always
    produce identical output. Never raises."""
    prior = next((d for d in last_7_days if d.get("verdict") != "pending"), None)
    parts: list[str] = []
    if prior is not None:
        phrase = _VERDICT_PHRASE.get(prior["verdict"])
        if phrase:
            parts.append(phrase)

    parts.append(f"Today: {_format_prescription(today_workout)}.")
    if today_workout.get("description"):
        parts.append(today_workout["description"])

    if days_to_race is not None:
        parts.append(f"{days_to_race} days to your {goal_type}.")
    else:
        parts.append(f"Working toward your {goal_type}.")

    return " ".join(parts)
