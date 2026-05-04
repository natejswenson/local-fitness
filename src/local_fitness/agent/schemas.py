"""Pydantic schemas for agent-produced structured output.

The brief is a small JSON structure: an ordered list of Takeaways. Each
Takeaway has the headline you see at a glance, a tone for visual
treatment, an optional metric pointer for the embedded chart, and a
markdown deep-dive that's hidden until the user expands the card.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Whitelisted metric names — must be queryable via /api/metric/{name} or
# /api/training-load. Keep this list aligned with the FastAPI _ALLOWED_METRICS.
MetricName = Literal[
    "rhr",
    "sleep_seconds",
    "sleep_score",
    "avg_stress",
    "body_battery_max",
    "body_battery_min",
    "vo2_max",
    "steps",
    "intensity_minutes_moderate",
    "intensity_minutes_vigorous",
    # Training-load model — the API exposes these under /api/training-load
    "ctl",
    "atl",
    "tsb",
]

Tone = Literal["positive", "caution", "critical", "neutral"]


def _collapse_whitespace(v: object) -> object:
    """Strip ALL whitespace (incl. internal) from a string. Used for enum
    fields where the model has been observed to emit raw newlines mid-value
    (e.g. ``"r\\nhr"`` instead of ``"rhr"``). Internal whitespace is never
    legitimate in our enums."""
    return "".join(v.split()) if isinstance(v, str) else v


def _strip_edges(v: object) -> object:
    """Strip leading/trailing whitespace only. Used for free-form prose
    fields where internal newlines may be intentional (markdown details)."""
    return v.strip() if isinstance(v, str) else v


class TakeawayMetric(BaseModel):
    metric: MetricName
    days: int = Field(14, ge=7, le=730, description="Window for the inline chart")

    @field_validator("metric", mode="before")
    @classmethod
    def _clean_metric(cls, v: object) -> object:
        return _collapse_whitespace(v)


class Takeaway(BaseModel):
    headline: str = Field(..., description="One-line action or insight, ~6-12 words")
    summary: str = Field(..., description="One-line 'why' — the data backing it, ~10-25 words")
    tone: Tone = Field("neutral", description="Sentiment for color treatment")
    metric: TakeawayMetric | None = Field(None, description="What metric to chart inline")
    details: str = Field(..., description="Full markdown deep-dive shown when expanded")

    @field_validator("tone", mode="before")
    @classmethod
    def _clean_tone(cls, v: object) -> object:
        # Enum field — collapse internal whitespace too. Models occasionally
        # emit ``"\ncritical"`` or ``"crit\nical"`` from streamed JSON.
        return _collapse_whitespace(v)

    @field_validator("headline", "summary", "details", mode="before")
    @classmethod
    def _trim_prose(cls, v: object) -> object:
        # Free-form fields: trim leading/trailing whitespace but preserve
        # internal newlines (markdown details may legitimately contain them).
        return _strip_edges(v)


class Brief(BaseModel):
    date: str
    user_name: str
    # ISO timestamp of when this brief was generated. Optional so older
    # on-disk briefs (pre-2026-04-27) still load. Used by the UI to detect
    # when newer data has landed since the brief was written.
    generated_at: str | None = None
    takeaways: list[Takeaway] = Field(..., min_length=1, max_length=5)
