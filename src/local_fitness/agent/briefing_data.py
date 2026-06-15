"""Pre-fetch the brief's standard data by CALLING the existing tool handlers.

The daily brief otherwise makes ~8 sequential model round-trips to gather the
same data every time. This module runs those same tool handlers *in-process*
(no model round-trip) and assembles their results into one bundle that gets
injected into the brief prompt, collapsing the gather phase to a single
generation pass.

Design choice (post red-team/siege): we call the audited tool handlers and
unwrap their JSON rather than re-implementing the queries — so the frozen-set
SQL validation never moves, and the bundle is byte-identical to what the model
would fetch via tool calls. The only thing saved is the latency of the
round-trips.
"""
from __future__ import annotations

import json

from . import tools

#: Trends the brief always needs. Includes the recovery signals
#: (body_battery_max, avg_stress) the HR/recovery mandate leans on — without
#: them the model re-fetches on exactly the high-signal days. Every entry is a
#: member of DAILY_NUMERIC_METRICS and flows through the handler's own
#: frozen-set validation; this list is a frozen constant, never a parameter.
_TREND_METRICS = ("sleep_seconds", "steps", "rhr", "body_battery_max", "avg_stress")


def _unwrap(resp: dict):
    """Decode a tool handler's `{"content":[{"text": json}]}` envelope."""
    return json.loads(resp["content"][0]["text"])


async def gather_brief_context() -> dict:
    """Run the brief's standard tools in-process; return a bundle keyed by the
    originating tool name (so the prompt's mandates resolve without bridging).

    Empty/sparse data is self-documenting: a handler returns
    `{"error": "..."}` for a window with no rows, and that envelope is carried
    verbatim — the model sees the same "no data" signal it would from a live
    call. Sub-queries are not aborted on a single miss.
    """
    bundle: dict = {
        "get_today_status": _unwrap(await tools.get_today_status.handler({})),
        "training_load_status": _unwrap(await tools.training_load_status.handler({})),
        "find_anomalies.rhr": _unwrap(await tools.find_anomalies.handler({"metric": "rhr"})),
        "query_workouts.14d": _unwrap(
            await tools.query_workouts.handler({"days": 14, "limit": 20})
        ),
        "get_training_plan_status": _unwrap(
            await tools.get_training_plan_status.handler({})
        ),
    }
    for metric in _TREND_METRICS:
        bundle[f"get_metric_trend.{metric}"] = _unwrap(
            await tools.get_metric_trend.handler({"metric": metric, "days": 14})
        )
    return bundle


def render_for_prompt(bundle: dict) -> str:
    """Compact JSON for injection (matches the `_text` format the model parses)."""
    return json.dumps(bundle, default=str)
