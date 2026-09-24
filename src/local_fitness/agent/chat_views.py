"""Pure Markdown views of computed tool payloads; never fetch or regrade data."""
from __future__ import annotations

import html
import re
from collections import defaultdict
from datetime import date, timedelta

from . import units
from .render import render_table

_METRIC_LABELS = {
    "rhr": "Resting heart rate", "sleep_seconds": "Sleep",
    "sleep_score": "Sleep score", "avg_stress": "Average stress",
    "max_stress": "Peak stress", "body_battery_max": "Body Battery peak",
    "body_battery_min": "Body Battery low", "body_battery_charged": "Battery charged",
    "body_battery_drained": "Battery drained", "steps": "Steps",
    "avg_spo2": "Blood oxygen", "respiration_avg": "Respiration",
    "vo2_max": "VO₂ max", "ctl": "Fitness (CTL)", "atl": "Fatigue (ATL)",
    "tsb": "Freshness (TSB)", "active_calories": "Active calories",
    "intensity_minutes_weighted": "Weighted intensity minutes",
}
_PRIORITY = ("rhr", "sleep_seconds", "sleep_score", "body_battery_max", "avg_stress", "steps")


def _plain(value: object) -> str:
    """Keep names/descriptions literal, on one line, with no links or HTML."""
    text = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#!])", r"\\\1", text)


def metric_label(metric: str) -> str:
    return _METRIC_LABELS.get(metric, metric.replace("_seconds", "").replace("_", " ").capitalize())


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def metric_value(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    if metric.startswith("sleep_") and metric.endswith("seconds"):
        return units.format_hm(value) or "—"
    suffix = {"rhr": " bpm", "avg_spo2": "%", "respiration_avg": " /min",
              "active_calories": " kcal", "vo2_max": " ml/kg/min"}.get(metric, "")
    if metric.startswith("intensity_minutes"):
        suffix = " min"
    return _number(value) + suffix


def _distance(workout: dict, prefix: str) -> str | None:
    miles = workout.get(f"{prefix}_distance_mi")
    meters = workout.get(f"{prefix}_distance_m")
    if miles is not None:
        return f"{miles:g} mi"
    if meters is not None:
        return f"{meters / 1000:g} km"
    return None


def _prescription(workout: dict) -> str:
    parts = [_plain(workout.get("type") or "session")]
    distance = _distance(workout, "target")
    if distance:
        parts.append(distance)
    duration = workout.get("target_duration_formatted") or units.format_duration(workout.get("target_duration_sec"))
    if duration:
        parts.append(duration)
    pace = workout.get("target_pace_min_per_mi") or units.format_pace_min_per_mi(workout.get("target_pace_sec_per_km"))
    if pace:
        parts.append(f"{pace}/mi")
    if workout.get("target_hr_max") is not None:
        parts.append(f"HR ≤ {workout['target_hr_max']:g} bpm")
    return " · ".join(parts)


def render_snapshot(payload: dict) -> str:
    today = payload["date"]
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    lines = [f"## Daily snapshot · {today}", ""]
    metrics = payload.get("metrics") or []
    rows_by_date = defaultdict(list)
    missing = []
    provisional = False
    ordered = sorted(metrics, key=lambda m: (
        _PRIORITY.index(m["metric"]) if m["metric"] in _PRIORITY else len(_PRIORITY), m["metric"]))
    for metric in ordered:
        key = metric["metric"]
        label = metric_label(key)
        value = metric.get("value")
        excluded = metric.get("partial_today_excluded") or metric.get("provisional_today_excluded")
        value_date = yesterday if excluded else today
        context = []
        baseline = metric.get("baseline")
        if baseline is not None:
            context.append(f"usual {metric_value(key, baseline)}")
        if metric.get("delta_pct") is not None:
            context.append(f"{metric.get('arrow') or ''} {metric['delta_pct']:+g}%".strip())
        elif metric.get("treatment") == "trend_arrow" and metric.get("arrow"):
            context.append(f"7-day trend {metric['arrow']}")
        if metric.get("partial_today"):
            context.append("partial total")
        if metric.get("provisional_today"):
            context.append("provisional")
            provisional = True
        if value is not None:
            rows_by_date[value_date].append([label, metric_value(key, value), " · ".join(context) or "—"])
        else:
            missing.append(f"{label} ({value_date})")
        if metric.get("provisional_today_value") is not None:
            rows_by_date[today].append([label, metric_value(key, metric["provisional_today_value"]),
                                       "provisional"])
            provisional = True
    if rows_by_date:
        for day, label in ((yesterday, "Yesterday"), (today, "Today")):
            if rows_by_date.get(day):
                lines.extend([f"### {label} · {day}", "",
                              render_table(["Metric", "Value", "Context"], rows_by_date[day]), ""])
        if missing:
            lines.extend(["Unavailable: " + "; ".join(missing) + ".", ""])
    else:
        lines.extend(["No daily readings available. Ask to sync Garmin to get started.", ""])
    if provisional:
        lines.extend(["Today's provisional readings may change and are excluded from comparisons. "
                      "Ask to sync Garmin for an updated reading.", ""])
    if payload.get("data_as_of"):
        lines.extend([f"Last sync covering today: {_plain(payload['data_as_of'])}.", ""])

    load = payload.get("training_load") or {}
    lines.extend(["### Training load", ""])
    if all(load.get(k) is None for k in ("ctl", "atl", "tsb")):
        lines.extend(["No training-load data yet.", ""])
    else:
        # as_of dates the pipeline, NOT these last-complete-day values.
        stamp = load.get("current_form_date")
        lines.extend([f"Current form · {stamp or 'date unavailable'} · {_plain(load.get('interpretation') or 'unavailable')}", "",
                      render_table(["Fitness", "Fatigue", "Freshness"], [[_number(load.get(k)) for k in ("ctl", "atl", "tsb")]]), ""])
    if (load.get("baseline_stale_days") or 0) > 0:
        lines.extend([f"Training-load sync is {load['baseline_stale_days']} days stale (last updated {load.get('as_of')}). Ask to sync Garmin.", ""])

    lines.extend(["### Recent workouts", ""])
    for workout in payload.get("recent_workouts") or []:
        parts = [_plain(workout.get("date", "")), _plain(workout.get("activity_name") or workout.get("activity_type") or "Workout")]
        if workout.get("effort"):
            parts.append(f"classified as {_plain(workout['effort'])}")
        if workout.get("distance_mi") is not None:
            parts.append(f"{workout['distance_mi']:g} mi")
        elif workout.get("distance_meters") is not None:
            parts.append(f"{workout['distance_meters'] / 1000:g} km")
        if workout.get("duration_formatted"):
            parts.append(workout["duration_formatted"])
        if workout.get("pace_min_per_mi"):
            parts.append(f"{workout['pace_min_per_mi']}/mi")
        if workout.get("avg_hr") is not None:
            parts.append(f"{workout['avg_hr']:g} bpm")
        lines.append("- " + " · ".join(parts))
    if not payload.get("recent_workouts"):
        lines.append("No workouts logged yet.")
    if (payload.get("brief_stale_days") or 0) > 0:
        lines.extend(["", f"Latest saved brief: {payload['latest_brief_date']} ({payload['brief_stale_days']} days old)."])
    if payload.get("memory_status"):
        lines.extend(["", "Saved coaching preferences are temporarily unavailable."])
    return "\n".join(lines)


def _plan_intro(payload: dict) -> list[str]:
    lines = ["## Training plan", ""]
    if not payload.get("active"):
        lines.extend(["No active training plan.", ""])
    else:
        if payload.get("title"):
            lines.extend([f"**{_plain(payload['title'])}**", ""])
        parts = [_plain(payload.get("goal_type") or "Active plan")]
        if payload.get("race_date"):
            date_label = "ends" if payload.get("goal_type") == "custom" else "race"
            parts.append(f"{date_label} {payload['race_date']}")
        if payload.get("target_time_formatted"):
            parts.append(f"target {payload['target_time_formatted']}")
        lines.extend([" · ".join(parts), ""])
        adherence = []
        for key, label in (("adherence_pct", "All days"), ("sessions_adherence_pct", "Sessions")):
            if payload.get(key) is not None:
                adherence.append(f"{label}: {payload[key]:g}%")
        if adherence:
            lines.extend(["Whole-plan adherence · " + " · ".join(adherence), ""])
        through = payload.get("data_through")
        lines.extend([f"Daily data through {through or 'unknown'}. Pending means not yet graded.", ""])
        if not through or through < payload.get("as_of", through):
            lines.extend(["Data does not cover today. Ask to sync Garmin before judging missing sessions.", ""])
    draft = payload.get("pending_draft")
    if draft:
        lines.extend([f"**Draft awaiting a decision** · {_plain(draft.get('title') or 'Untitled plan')} · {draft.get('workout_count', 0)} sessions",
                      "", "This draft is not active. Review it before choosing to activate or discard it.", ""])
    elif not payload.get("active"):
        next_step = ("Ask to create a plan around your goal and available training days."
                     if "pending_draft" in payload else
                     "Ask for plan status to check for a draft before creating a plan.")
        lines.extend([next_step, ""])
    return lines


def render_plan_status(payload: dict) -> str:
    lines = _plan_intro(payload)
    if not payload.get("active"):
        return "\n".join(lines).rstrip()
    for key, title in (("today", "Today"), ("last_graded", "Last graded")):
        workout = payload.get(key)
        if not workout:
            lines.extend([f"{title}: no session recorded in this summary.", ""])
            continue
        lines.extend([f"### {title} · {workout['date']} · session {workout.get('seq') or 1}", "",
                      _prescription(workout) + f" · {_plain(workout.get('verdict') or 'pending')}", ""])
        description = workout.get("description_full") or workout.get("description")
        if description:
            lines.extend([_plain(description), ""])
    lines.append("This is a single-session summary. Ask for plan progress to see every session, including double days.")
    return "\n".join(lines)


def render_plan_progress(payload: dict) -> str:
    lines = _plan_intro(payload)
    if not payload.get("active"):
        return "\n".join(lines).rstrip()
    predicted = payload.get("predicted_finish_formatted")
    if predicted:
        confidence = _plain(payload.get("projection_confidence") or "unavailable")
        lines.extend([f"Projected finish: {predicted} · confidence {confidence} (estimate).", ""])
        basis = payload.get("projection_basis") or {}
        if basis:
            lines.extend([f"Based on {basis.get('date')} · {_number(basis.get('distance_mi'))} mi · "
                          f"{basis.get('pace_min_per_mi') or '—'}/mi · extrapolation {_number(basis.get('extrapolation_ratio'))}×.", ""])
        gap = payload.get("goal_gap")
        if gap and gap.get("gap_formatted"):
            lines.extend([f"Projected gap: {gap['gap_formatted']} (positive = slower than goal).", ""])
    elif payload.get("goal_type") != "custom" or payload.get("target_time_formatted"):
        lines.extend(["Projected finish unavailable: no qualifying effort or goal distance.", ""])
    week = payload.get("this_week") or {}
    if week:
        lines.extend(["### Trailing 7 days", "",
                      f"On-foot miles: {_number(week.get('week_actual_mi'))} / {_number(week.get('week_planned_mi'))} planned · "
                      f"run {_number(week.get('week_run_mi'))} · walk {_number(week.get('week_walk_mi'))}", ""])
    window = payload.get("workout_window") or {}
    scope = "Full plan" if window.get("full") else "Displayed window"
    lines.extend([f"{scope}: {window.get('start') or '—'} to {window.get('end') or '—'}.", ""])
    if not window.get("full"):
        lines.extend(["Ask for the full plan to include sessions outside this window.", ""])
    by_date = defaultdict(list)
    for workout in payload.get("workouts") or []:
        by_date[workout["date"]].append(workout)
    if not by_date:
        lines.append("No prescribed sessions in this window.")
    for day, workouts in sorted(by_date.items()):
        lines.extend([f"### {day}", ""])
        for workout in sorted(workouts, key=lambda w: w.get("seq", 1)):
            lines.append(f"- Session {workout.get('seq', 1)} · {_prescription(workout)} · {_plain(workout.get('verdict') or 'pending')}")
            if workout.get("description"):
                lines.append(f"  {_plain(workout['description'])}")
        actual = _distance(workouts[0], "actual")
        today = payload.get("as_of")
        if actual is not None and (not today or day <= today):
            label = "So far today" if day == today else "Day total"
            scope = "running + walking" + ("; shared across sessions" if len(workouts) > 1 else "")
            lines.extend(["", f"{label}: {actual} on foot ({scope})."])
        lines.append("")
    return "\n".join(lines).rstrip()


RENDERERS = {"daily_snapshot": render_snapshot,
             "get_training_plan_status": render_plan_status,
             "get_training_plan_progress": render_plan_progress}
