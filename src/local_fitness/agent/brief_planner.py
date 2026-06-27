"""Deterministic brief PLANNER — the tested half of the agent/code separation.

This relocates the brief prompt's *reasoning* (context selection, threshold
evaluation, priority, an advisory tone prior) out of the LLM and into code
covered by tests. It produces a typed :class:`~.schemas.BriefContext` that — once
Phase 3 cuts over — is the SOLE input to a toolless generator. Through Phase 2
nothing consumes this in production; the prompt stays authoritative.

What lives here (extracted verbatim from ``agent/prompts.py`` Step-2 mandates):
  * trigger predicates — pure functions, one named threshold block (``_TRIGGERS``)
  * a FIXED priority rank (``_PRIORITY``) — the prompt's order, no float salience
  * ``suggest_tone`` — an ADVISORY tone prior the generator may override

The voice (exemplars, prose-craft, holistic precedence/continuity) is the
irreducible LLM half and deliberately stays in the prompt — only the *selection
thresholds* move here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .. import db
from . import status as status_mod
from . import units
from .coach import CoachProfile, resolve_coach_profile
from .schemas import BriefContext, CandidateTakeaway, GroundedValue, TakeawayMetric, Tone

# --- one named threshold block (the prompt's tuning knobs, now testable) ---
# Each maps to a line in agent/prompts.py:381-489. Tune here, not in prose.
_TRIGGERS = {
    # Conditioning mandate (prompts.py:398-430)
    "ctl_change_pct": 5.0,       # CTL moved >5% over 14d (up or down)
    "run_count_delta": 3,        # 14d run count materially != prior 14d (|Δ| ≥ 3)
    "run_gap_days": 5,           # 5+ days since the last run
    "te_collapse": 1.0,          # recent runs all "filler" (aerobic TE < 1.0)
    # HR & recovery mandate (prompts.py:443-478)
    "rhr_elevated_bpm": 3,       # RHR ≥ 3 bpm above baseline ...
    "rhr_elevated_days": 3,      # ... for 3+ days
    "sleep_score_low": 65,       # sleep score under 65
    "sleep_short_seconds": 3600, # 1h+ below the 60-day sleep average
    "bb_low_max": 50,            # body battery topping under 50 ...
    "bb_low_nights": 3,          # ... for 3+ nights
    "stress_7d_high": 40,        # 7-day stress average above 40
    # Training-load freshness bands (status._tsb_interpretation)
    "tsb_fresh": 5.0,            # TSB > +5 = fresh / green light
    "tsb_very_fatigued": -20.0,  # TSB < -20 = very fatigued
}

# Fixed priority — the prompt's order (prompts.py:251-271). Lower = earlier.
_PRIORITY = {"workout": 0, "steps": 1, "conditioning": 2, "recovery": 3, "wildcard": 4}

_LOOKBACK_DAYS = 14
_RUNNING_TYPES = ("running", "trail_running", "treadmill_running", "track_running")


# --- signals: everything the predicates + tone resolvers read --------------
@dataclass(frozen=True)
class Signals:
    """Computed, deterministic read of the DB the predicates operate on. All
    fields default so unit tests construct only what a predicate needs."""
    tsb: float | None = None
    ctl: float | None = None
    atl: float | None = None
    ctl_pct_change_14d: float | None = None
    rhr_today: float | None = None
    rhr_baseline_mean: float | None = None
    rhr_days_elevated: int = 0
    sleep_today_seconds: float | None = None
    sleep_baseline_mean: float | None = None
    sleep_score_today: float | None = None
    bb_max_today: float | None = None
    bb_low_nights: int = 0
    stress_7d_avg: float | None = None
    steps_yesterday: float | None = None
    steps_7d_avg: float | None = None
    step_goal: int | None = None
    runs_14d: int = 0
    runs_prior_14d: int = 0
    days_since_last_run: int | None = None
    recent_te: tuple[float, ...] = ()
    anomalies: tuple[dict, ...] = ()
    plan_today: dict | None = None
    days_to_race: int | None = None


# --- conditioning predicates (prompts.py:398-430) --------------------------
def ctl_shifted(ctl_pct_change_14d: float | None) -> bool:
    """CTL has changed more than ±5% in the last 14 days."""
    return ctl_pct_change_14d is not None and abs(ctl_pct_change_14d) > _TRIGGERS["ctl_change_pct"]


def run_count_shifted(runs_14d: int, runs_prior_14d: int) -> bool:
    """14-day run count is materially different from the prior fortnight."""
    return abs(runs_14d - runs_prior_14d) >= _TRIGGERS["run_count_delta"]


def te_collapsing(recent_te: tuple[float, ...]) -> bool:
    """The last few runs are all zone-2 filler (every aerobic TE < 1.0)."""
    return len(recent_te) >= 3 and all(te < _TRIGGERS["te_collapse"] for te in recent_te)


def long_run_absence(days_since_last_run: int | None) -> bool:
    """5+ days since the last run (or never ran)."""
    return days_since_last_run is None or days_since_last_run >= _TRIGGERS["run_gap_days"]


def conditioning_fires(sig: Signals) -> bool:
    return (
        ctl_shifted(sig.ctl_pct_change_14d)
        or run_count_shifted(sig.runs_14d, sig.runs_prior_14d)
        or te_collapsing(sig.recent_te)
        or long_run_absence(sig.days_since_last_run)
    )


# --- recovery predicates (prompts.py:443-478) ------------------------------
def rhr_elevated(rhr_today: float | None, baseline_mean: float | None, days_elevated: int) -> bool:
    """RHR ≥ 3 bpm above baseline AND has been for 3+ days."""
    if rhr_today is None or baseline_mean is None:
        return False
    return (rhr_today - baseline_mean) >= _TRIGGERS["rhr_elevated_bpm"] and \
        days_elevated >= _TRIGGERS["rhr_elevated_days"]


def rhr_green(rhr_today: float | None, baseline_mean: float | None,
              sleep_score: float | None, stress_7d_avg: float | None) -> bool:
    """RHR meaningfully below baseline AND sleep solid AND stress low."""
    if rhr_today is None or baseline_mean is None:
        return False
    return (
        rhr_today < baseline_mean
        and (sleep_score is None or sleep_score >= _TRIGGERS["sleep_score_low"])
        and (stress_7d_avg is None or stress_7d_avg <= _TRIGGERS["stress_7d_high"])
    )


def sleep_poor(sleep_today_seconds: float | None, sleep_baseline_mean: float | None,
               sleep_score: float | None) -> bool:
    """Sleep crashed (1h+ below the 60-day average) OR sleep score under 65."""
    if sleep_score is not None and sleep_score < _TRIGGERS["sleep_score_low"]:
        return True
    if sleep_today_seconds is not None and sleep_baseline_mean is not None:
        return (sleep_baseline_mean - sleep_today_seconds) >= _TRIGGERS["sleep_short_seconds"]
    return False


def bb_or_stress_low(bb_low_nights: int, stress_7d_avg: float | None) -> bool:
    """Body battery topping under 50 for 3+ nights OR 7-day stress avg > 40."""
    if bb_low_nights >= _TRIGGERS["bb_low_nights"]:
        return True
    return stress_7d_avg is not None and stress_7d_avg > _TRIGGERS["stress_7d_high"]


def recovery_anomaly(anomalies: tuple[dict, ...]) -> bool:
    return len(anomalies) > 0


def recovery_fires(sig: Signals) -> bool:
    return (
        rhr_elevated(sig.rhr_today, sig.rhr_baseline_mean, sig.rhr_days_elevated)
        or sleep_poor(sig.sleep_today_seconds, sig.sleep_baseline_mean, sig.sleep_score_today)
        or bb_or_stress_low(sig.bb_low_nights, sig.stress_7d_avg)
        or recovery_anomaly(sig.anomalies)
        or rhr_green(sig.rhr_today, sig.rhr_baseline_mean, sig.sleep_score_today, sig.stress_7d_avg)
    )


def recovery_all_green(sig: Signals) -> bool:
    """All recovery signals green AND nothing else flagged → roll into workout,
    do NOT write a standalone 'you're fine' card (prompts.py:475-478)."""
    return rhr_green(sig.rhr_today, sig.rhr_baseline_mean, sig.sleep_score_today, sig.stress_7d_avg) \
        and not rhr_elevated(sig.rhr_today, sig.rhr_baseline_mean, sig.rhr_days_elevated) \
        and not sleep_poor(sig.sleep_today_seconds, sig.sleep_baseline_mean, sig.sleep_score_today) \
        and not bb_or_stress_low(sig.bb_low_nights, sig.stress_7d_avg) \
        and not recovery_anomaly(sig.anomalies)


# --- advisory tone (prompts.py:308-478) — generator may override -----------
def _mv(metrics: list[GroundedValue], name: str, default: float | None = None) -> float | None:
    for m in metrics:
        if m.name == name:
            return m.value
    return default


def _workout_tone(metrics: list[GroundedValue]) -> Tone:
    """Workout tone branches (prompts.py:308-345)."""
    tsb = _mv(metrics, "tsb")
    ctl_pct = _mv(metrics, "ctl_pct_change_14d")
    days_since_run = _mv(metrics, "days_since_last_run")
    recovery_red = _mv(metrics, "recovery_red", 0) or 0
    rhr_green_flag = _mv(metrics, "rhr_green", 0) or 0
    # Recovery red flags take precedence → ease off (caution).
    if recovery_red or (tsb is not None and tsb < _TRIGGERS["tsb_very_fatigued"]):
        return "caution"
    # Fitness clearly sliding AND no recent training → harsh push.
    if (ctl_pct is not None and ctl_pct < -_TRIGGERS["ctl_change_pct"]) and \
            (days_since_run is not None and days_since_run >= _TRIGGERS["run_gap_days"]):
        return "critical"
    # Fresh OR recovery-green light → celebrate, push (prompts.py:310-316: a green
    # light is driven by recovery being clear, not TSB alone).
    if (tsb is not None and tsb > _TRIGGERS["tsb_fresh"]) or rhr_green_flag:
        return "positive"
    return "neutral"


def _conditioning_tone(metrics: list[GroundedValue]) -> Tone:
    """Conditioning tone branches (prompts.py:410-430)."""
    ctl_pct = _mv(metrics, "ctl_pct_change_14d")
    days_since_run = _mv(metrics, "days_since_last_run")
    if days_since_run is not None and days_since_run >= _TRIGGERS["run_gap_days"]:
        return "critical"            # long absence
    if ctl_pct is not None and ctl_pct < -_TRIGGERS["ctl_change_pct"]:
        return "critical"            # sliding
    if ctl_pct is not None and ctl_pct > _TRIGGERS["ctl_change_pct"]:
        return "positive"            # trending up
    return "neutral"                 # stalled


def _recovery_tone(metrics: list[GroundedValue]) -> Tone:
    """Recovery tone branches (prompts.py:448-478)."""
    rhr_delta = _mv(metrics, "rhr_delta_bpm")
    days_elevated = _mv(metrics, "rhr_days_elevated", 0) or 0
    sleep_score = _mv(metrics, "sleep_score")
    sleep_short = _mv(metrics, "sleep_short_seconds", 0) or 0
    bb_low_nights = _mv(metrics, "bb_low_nights", 0) or 0
    stress_7d = _mv(metrics, "stress_7d_avg")
    rhr_high = rhr_delta is not None and rhr_delta >= _TRIGGERS["rhr_elevated_bpm"]
    sleep_bad = (sleep_score is not None and sleep_score < _TRIGGERS["sleep_score_low"]) \
        or sleep_short >= _TRIGGERS["sleep_short_seconds"]
    bb_bad = bb_low_nights >= _TRIGGERS["bb_low_nights"] or \
        (stress_7d is not None and stress_7d > _TRIGGERS["stress_7d_high"])
    # Sustained elevated RHR with another red flag → harsh (critical).
    if rhr_high and days_elevated >= _TRIGGERS["rhr_elevated_days"] and (sleep_bad or bb_bad):
        return "critical"
    if rhr_high or sleep_bad or bb_bad:
        return "caution"
    if rhr_delta is not None and rhr_delta < 0:  # below baseline, all clear
        return "positive"
    return "neutral"


def _steps_tone(frac_of_goal: float, avg_frac_of_goal: float, includes_harsh: bool) -> Tone:
    """Steps tone branches (prompts.py:386-394 + the harsh/soft missed block)."""
    if frac_of_goal >= 1.0:
        return "positive" if avg_frac_of_goal >= 1.0 else "caution"
    # Missed goal: harshness gate decides critical vs caution (coach.includes_harsh_block).
    return "critical" if includes_harsh else "caution"


def suggest_tone(category: str, metrics: list[GroundedValue], profile: CoachProfile) -> Tone:
    """Advisory tone prior for a candidate. The generator may override it
    (recovery-precedence and continuity-escalation stay LLM judgment)."""
    if category == "workout":
        return _workout_tone(metrics)
    if category == "conditioning":
        return _conditioning_tone(metrics)
    if category == "recovery":
        return _recovery_tone(metrics)
    if category == "steps":
        frac = _mv(metrics, "frac_of_goal", 1.0) or 1.0
        avg = _mv(metrics, "avg_frac_of_goal", 1.0) or 1.0
        return _steps_tone(frac, avg, profile.includes_harsh_block)
    return "neutral"


# --- GroundedValue rendering ----------------------------------------------
def _gv(name: str, value: float | None, unit: str, display: str) -> GroundedValue | None:
    if value is None:
        return None
    return GroundedValue(name=name, value=float(value), unit=unit, display=display)


def _round(v: float) -> float:
    return round(v, 1)


# --- candidate builders ----------------------------------------------------
def _workout_candidate(sig: Signals, profile: CoachProfile) -> CandidateTakeaway:
    recovery_red = 1.0 if (
        rhr_elevated(sig.rhr_today, sig.rhr_baseline_mean, sig.rhr_days_elevated)
        or sleep_poor(sig.sleep_today_seconds, sig.sleep_baseline_mean, sig.sleep_score_today)
        or bb_or_stress_low(sig.bb_low_nights, sig.stress_7d_avg)
    ) else 0.0
    rhr_green_flag = 1.0 if rhr_green(
        sig.rhr_today, sig.rhr_baseline_mean, sig.sleep_score_today, sig.stress_7d_avg) else 0.0
    metrics = [m for m in (
        _gv("tsb", sig.tsb, "none", _signed(sig.tsb)),
        _gv("ctl", sig.ctl, "none", _plain(sig.ctl)),
        _gv("ctl_pct_change_14d", sig.ctl_pct_change_14d, "pct", _signed(sig.ctl_pct_change_14d)),
        _gv("days_since_last_run", sig.days_since_last_run, "count", _plain(sig.days_since_last_run)),
        _gv("recovery_red", recovery_red, "none", _plain(recovery_red)),
        _gv("rhr_green", rhr_green_flag, "none", _plain(rhr_green_flag)),
    ) if m is not None]
    triggers = ["workout_mandate"]
    if sig.plan_today:
        triggers.append("active_plan")
    return CandidateTakeaway(
        category="workout", fired_triggers=triggers, metrics=metrics,
        suggested_tone=suggest_tone("workout", metrics, profile),
        chart_metric=TakeawayMetric(metric="tsb", days=30),
        evidence=_workout_evidence(sig),
    )


def _steps_candidate(sig: Signals, profile: CoachProfile) -> CandidateTakeaway:
    goal = sig.step_goal or 0
    frac = (sig.steps_yesterday / goal) if (goal and sig.steps_yesterday is not None) else 1.0
    avg_frac = (sig.steps_7d_avg / goal) if (goal and sig.steps_7d_avg is not None) else 1.0
    metrics = [m for m in (
        _gv("steps_yesterday", sig.steps_yesterday, "steps", _commas(sig.steps_yesterday)),
        _gv("steps_7d_avg", sig.steps_7d_avg, "steps", _commas(sig.steps_7d_avg)),
        _gv("frac_of_goal", _round(frac), "pct", f"{frac * 100:.0f}%"),
        _gv("avg_frac_of_goal", _round(avg_frac), "pct", f"{avg_frac * 100:.0f}%"),
    ) if m is not None]
    fired = ["steps_mandate"]
    if frac < 1.0:
        fired.append("under_goal")
    elif avg_frac < 1.0:
        fired.append("avg_slipping")
    return CandidateTakeaway(
        category="steps", fired_triggers=fired, metrics=metrics,
        suggested_tone=suggest_tone("steps", metrics, profile),
        chart_metric=TakeawayMetric(metric="steps", days=14),
        evidence=f"yesterday {_commas(sig.steps_yesterday)} vs {goal:,} goal",
    )


def _conditioning_candidate(sig: Signals, profile: CoachProfile) -> CandidateTakeaway | None:
    if not conditioning_fires(sig):
        return None
    fired = []
    if ctl_shifted(sig.ctl_pct_change_14d):
        fired.append("ctl_shifted")
    if run_count_shifted(sig.runs_14d, sig.runs_prior_14d):
        fired.append("run_count_shifted")
    if te_collapsing(sig.recent_te):
        fired.append("te_collapsing")
    if long_run_absence(sig.days_since_last_run):
        fired.append("long_run_absence")
    metrics = [m for m in (
        _gv("ctl", sig.ctl, "none", _plain(sig.ctl)),
        _gv("ctl_pct_change_14d", sig.ctl_pct_change_14d, "pct", _signed(sig.ctl_pct_change_14d)),
        _gv("days_since_last_run", sig.days_since_last_run, "count", _plain(sig.days_since_last_run)),
        _gv("runs_14d", sig.runs_14d, "count", _plain(sig.runs_14d)),
    ) if m is not None]
    return CandidateTakeaway(
        category="conditioning", fired_triggers=fired, metrics=metrics,
        suggested_tone=suggest_tone("conditioning", metrics, profile),
        chart_metric=TakeawayMetric(metric="ctl", days=60),
        evidence="; ".join(fired),
    )


def _recovery_candidate(sig: Signals, profile: CoachProfile) -> CandidateTakeaway | None:
    # All-green with nothing else flagged → roll into the workout card, no standalone.
    if not recovery_fires(sig) or recovery_all_green(sig):
        return None
    rhr_delta = (sig.rhr_today - sig.rhr_baseline_mean) \
        if (sig.rhr_today is not None and sig.rhr_baseline_mean is not None) else None
    sleep_short = (sig.sleep_baseline_mean - sig.sleep_today_seconds) \
        if (sig.sleep_baseline_mean is not None and sig.sleep_today_seconds is not None) else None
    fired = []
    if rhr_elevated(sig.rhr_today, sig.rhr_baseline_mean, sig.rhr_days_elevated):
        fired.append("rhr_elevated")
    if sleep_poor(sig.sleep_today_seconds, sig.sleep_baseline_mean, sig.sleep_score_today):
        fired.append("sleep_poor")
    if bb_or_stress_low(sig.bb_low_nights, sig.stress_7d_avg):
        fired.append("bb_or_stress_low")
    if recovery_anomaly(sig.anomalies):
        fired.append("recovery_anomaly")
    metrics = [m for m in (
        _gv("rhr_delta_bpm", rhr_delta, "bpm", _signed(rhr_delta)),
        _gv("rhr_days_elevated", sig.rhr_days_elevated, "count", _plain(sig.rhr_days_elevated)),
        _gv("sleep_score", sig.sleep_score_today, "none", _plain(sig.sleep_score_today)),
        _gv("sleep_short_seconds", sleep_short, "sec", _hm(sleep_short)),
        _gv("bb_low_nights", sig.bb_low_nights, "count", _plain(sig.bb_low_nights)),
        _gv("stress_7d_avg", sig.stress_7d_avg, "none", _plain(sig.stress_7d_avg)),
    ) if m is not None]
    return CandidateTakeaway(
        category="recovery", fired_triggers=fired, metrics=metrics,
        suggested_tone=suggest_tone("recovery", metrics, profile),
        chart_metric=TakeawayMetric(metric=_recovery_chart(fired), days=14),
        evidence="; ".join(fired),
    )


def _wildcard_candidate(sig: Signals, profile: CoachProfile) -> CandidateTakeaway | None:
    # One slot: a recovery anomaly that isn't already the recovery lead, or
    # race week (days_to_race small). Kept conservative — at most one.
    if sig.days_to_race is not None and 0 <= sig.days_to_race <= 10:
        return CandidateTakeaway(
            category="wildcard", fired_triggers=["race_week"],
            metrics=[m for m in (_gv("days_to_race", sig.days_to_race, "count",
                                     _plain(sig.days_to_race)),) if m],
            suggested_tone="neutral", chart_metric=None,
            evidence=f"race in {sig.days_to_race} days",
        )
    return None


def _recovery_chart(fired: list[str]) -> str:
    if "sleep_poor" in fired:
        return "sleep_seconds"
    if "bb_or_stress_low" in fired:
        return "body_battery_max"
    return "rhr"


# --- small display renderers ----------------------------------------------
def _signed(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:+.1f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v):+d}"


def _plain(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _commas(v: float | None) -> str:
    return f"{int(v):,}" if v is not None else ""


def _hm(seconds: float | None) -> str:
    """Render a duration as the coach-voice ``"7h 28m"`` (no seconds) — how sleep
    is talked about, and so grounding's pool isn't polluted by stray seconds."""
    if seconds is None:
        return ""
    h, m = divmod(int(round(seconds)) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _workout_evidence(sig: Signals) -> str:
    bits = []
    if sig.tsb is not None:
        bits.append(f"TSB {_signed(sig.tsb)}")
    if sig.days_since_last_run is not None:
        bits.append(f"{sig.days_since_last_run}d since last run")
    if sig.plan_today:
        bits.append(f"plan: {sig.plan_today.get('type', '?')}")
    return ", ".join(bits) or "prescribe today's session"


# --- signal computation (DB reads) ----------------------------------------
def _running(activity_type: str | None) -> bool:
    return (activity_type or "").lower() in _RUNNING_TYPES


def _compute_signals(conn, today: str, baseline: dict | None, step_goal: int | None,
                     plan_today: dict | None, days_to_race: int | None) -> Signals:
    today_d = date.fromisoformat(today)
    rows = {
        r["date"]: dict(r) for r in conn.execute(
            "SELECT * FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
            ((today_d - timedelta(days=60)).isoformat(), today),
        ).fetchall()
    }
    today_row = rows.get(today, {})
    yest_row = rows.get((today_d - timedelta(days=1)).isoformat(), {})

    def _recent(field: str, days: int) -> list[float]:
        out = []
        for d in range(days):
            row = rows.get((today_d - timedelta(days=d)).isoformat())
            if row and row.get(field) is not None:
                out.append(row[field])
        return out

    rhr_mean = baseline.get("rhr_60day_mean") if baseline else None
    rhr_days_elevated = 0
    if rhr_mean is not None:
        for d in range(14):
            row = rows.get((today_d - timedelta(days=d)).isoformat())
            if row and row.get("rhr") is not None and \
                    (row["rhr"] - rhr_mean) >= _TRIGGERS["rhr_elevated_bpm"]:
                rhr_days_elevated += 1
            else:
                break  # consecutive run from today backward

    bb_low_nights = sum(
        1 for v in _recent("body_battery_max", _TRIGGERS["bb_low_nights"])
        if v < _TRIGGERS["bb_low_max"]
    )
    stress_recent = _recent("avg_stress", 7)
    steps_recent = _recent("steps", 7)

    # CTL % change over 14d from the baselines table.
    ctl_then = conn.execute(
        "SELECT ctl FROM baselines WHERE date <= ? AND ctl IS NOT NULL ORDER BY date DESC LIMIT 1",
        ((today_d - timedelta(days=_LOOKBACK_DAYS)).isoformat(),),
    ).fetchone()
    ctl_now = baseline.get("ctl") if baseline else None
    ctl_pct = None
    if ctl_now is not None and ctl_then and ctl_then["ctl"]:
        ctl_pct = round((ctl_now - ctl_then["ctl"]) / ctl_then["ctl"] * 100, 1)

    # Run history.
    acts = [dict(r) for r in conn.execute(
        "SELECT date, activity_type, aerobic_te FROM activities "
        "WHERE date <= ? ORDER BY date DESC", (today,),
    ).fetchall()]
    runs = [a for a in acts if _running(a["activity_type"])]
    days_since = None
    if runs:
        days_since = (today_d - date.fromisoformat(runs[0]["date"])).days
    w14 = (today_d - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    w28 = (today_d - timedelta(days=2 * _LOOKBACK_DAYS)).isoformat()
    runs_14d = sum(1 for a in runs if a["date"] > w14)
    runs_prior = sum(1 for a in runs if w28 < a["date"] <= w14)
    recent_te = tuple(a["aerobic_te"] for a in runs[:5] if a.get("aerobic_te") is not None)

    anomalies = _rhr_anomalies(rows, today_d, baseline)

    return Signals(
        tsb=baseline.get("tsb") if baseline else None,
        ctl=ctl_now,
        atl=baseline.get("atl") if baseline else None,
        ctl_pct_change_14d=ctl_pct,
        rhr_today=today_row.get("rhr"),
        rhr_baseline_mean=rhr_mean,
        rhr_days_elevated=rhr_days_elevated,
        sleep_today_seconds=today_row.get("sleep_seconds"),
        sleep_baseline_mean=baseline.get("sleep_seconds_60day_mean") if baseline else None,
        sleep_score_today=today_row.get("sleep_score"),
        bb_max_today=today_row.get("body_battery_max"),
        bb_low_nights=bb_low_nights,
        stress_7d_avg=round(sum(stress_recent) / len(stress_recent), 1) if stress_recent else None,
        steps_yesterday=yest_row.get("steps"),
        steps_7d_avg=round(sum(steps_recent) / len(steps_recent), 1) if steps_recent else None,
        step_goal=step_goal,
        runs_14d=runs_14d,
        runs_prior_14d=runs_prior,
        days_since_last_run=days_since,
        recent_te=recent_te,
        anomalies=tuple(anomalies),
        plan_today=plan_today,
        days_to_race=days_to_race,
    )


def _rhr_anomalies(rows: dict, today_d: date, baseline: dict | None) -> list[dict]:
    """RHR readings in the last 14d more than 2 SD above the 60-day mean."""
    if not baseline:
        return []
    mean = baseline.get("rhr_60day_mean")
    sd = baseline.get("rhr_60day_sd")
    if not mean or not sd:
        return []
    out = []
    for d in range(14):
        day = (today_d - timedelta(days=d)).isoformat()
        row = rows.get(day)
        if row and row.get("rhr") is not None and (row["rhr"] - mean) > 2 * sd:
            out.append({"date": day, "metric": "rhr", "value": row["rhr"],
                        "baseline": mean})
    return out


def _plan_today(db_path: Path | None, today: str) -> dict:
    """Mirror tools.get_training_plan_status with an injected `today`/db_path."""
    from .. import plans

    active = plans.get_active_plan(db_path)
    if active is None:
        return {"active": False}
    frontier = db.last_known_daily_date(db_path)
    dates = [w["date"] for w in active["workouts"]] or [today]
    start = min(dates)
    end = max([today, *dates] + ([frontier] if frontier else []))
    activities_by_date = plans.load_activities_by_date(start, end, db_path)
    cfg = plans.resolve_grading_config(db_path)
    return plans.build_plan_status(active, frontier, activities_by_date, today, cfg)


# --- snapshot / training_load / trends payload -----------------------------
_SNAPSHOT_UNITS = {
    "rhr": ("bpm", lambda v: f"{int(v)} bpm"),
    "sleep_seconds": ("sec", _hm),
    "sleep_score": ("none", _plain),
    "avg_stress": ("none", _plain),
    "max_stress": ("none", _plain),
    "body_battery_max": ("none", _plain),
    "body_battery_min": ("none", _plain),
    "steps": ("steps", _commas),
    "vo2_max": ("none", _plain),
    "intensity_minutes_moderate": ("min", _plain),
    "intensity_minutes_vigorous": ("min", _plain),
}


def _snapshot_values(metric_rows: list[dict]) -> list[GroundedValue]:
    out = []
    for row in metric_rows:
        name, value = row["metric"], row.get("value")
        if value is None or name not in _SNAPSHOT_UNITS:
            continue
        unit, render = _SNAPSHOT_UNITS[name]
        gv = _gv(name, value, unit, render(value))
        if gv is not None:
            out.append(gv)
    return out


# 60-day baseline reference values the brief routinely cites ("+6 above your 52
# baseline"). Exposed so the toolless generator quotes the REAL baseline instead
# of deriving it — and so grounding can trace that citation.
_BASELINE_RENDER = {
    "rhr_60day_mean": ("rhr_baseline", "bpm", lambda v: f"{int(round(v))} bpm"),
    "sleep_seconds_60day_mean": ("sleep_baseline", "sec", _hm),
    "body_battery_max_60day_mean": ("body_battery_max_baseline", "none", _plain),
    "stress_60day_mean": ("stress_baseline", "none", _plain),
}


def _baseline_values(baseline: dict | None) -> list[GroundedValue]:
    if not baseline:
        return []
    out = []
    for col, (name, unit, render) in _BASELINE_RENDER.items():
        v = baseline.get(col)
        gv = _gv(name, v, unit, render(v)) if v is not None else None
        if gv is not None:
            out.append(gv)
    return out


def _training_load_values(tl: dict) -> list[GroundedValue]:
    out = []
    for name, unit in (("ctl", "none"), ("atl", "none"), ("tsb", "none")):
        v = tl.get(name)
        disp = _signed(v) if name == "tsb" else _plain(v)
        gv = _gv(name, v, unit, disp)
        if gv is not None:
            out.append(gv)
    return out


# --- the public entry point ------------------------------------------------
def assemble_brief_context(db_path: Path | None = None, *, today: str | None = None,
                           notes: str | None = None,
                           recent_briefs: list[dict] | None = None) -> BriefContext:
    """Deterministically assemble the full :class:`BriefContext` from the DB +
    injected ``today``. Pure read; never raises on an empty DB. Through Phase 2
    nothing consumes this in production — the prompt is still authoritative."""
    today = today or date.today().isoformat()
    user_name = db.get_setting("user_name", "Nate", db_path=db_path) or "Nate"
    try:
        step_goal = int(db.get_setting("daily_step_goal", "10000", db_path=db_path) or "10000")
    except ValueError:
        step_goal = 10000
    profile = resolve_coach_profile(db_path=db_path)

    plan = _plan_today(db_path, today)
    plan_today = plan if plan.get("active") else None
    days_to_race = plan.get("days_to_race") if plan_today else None

    with db.connect(db_path) as conn:
        baseline = status_mod._baseline_row(conn, today)
        metric_rows = status_mod._metric_rows(conn, today, baseline)
        training_load = status_mod._training_load(baseline)
        sig = _compute_signals(conn, today, baseline, step_goal,
                               plan_today.get("today") if plan_today else None, days_to_race)
        workouts_14d = _workouts_payload(conn, today)

    candidates = [
        _workout_candidate(sig, profile),
        _steps_candidate(sig, profile),
        _conditioning_candidate(sig, profile),
        _recovery_candidate(sig, profile),
        _wildcard_candidate(sig, profile),
    ]
    candidates = [c for c in candidates if c is not None]
    candidates.sort(key=lambda c: _PRIORITY[c.category])

    continuity = _continuity(recent_briefs)

    return BriefContext(
        date=today, user_name=user_name, candidates=candidates,
        snapshot=_snapshot_values(metric_rows) + _baseline_values(baseline),
        training_load=_training_load_values(training_load),
        trends=_snapshot_values([r for r in metric_rows if r["metric"] in
                                 ("rhr", "sleep_score", "steps", "body_battery_max")]),
        workouts_14d=workouts_14d,
        anomalies=list(sig.anomalies),
        continuity=continuity,
        plan_today=plan_today,
        step_goal=step_goal,
        days_to_race=days_to_race,
    )


def _workouts_payload(conn, today: str) -> list[dict]:
    """The ACTUAL workouts in the last 14 days (most recent first) — date, type,
    distance, pace, duration, TE, load — so the toolless generator can cite
    'yesterday's 6-mile long run' concretely (was query_workouts(days=14); the
    summary counts live on the conditioning candidate's metrics)."""
    cutoff = (date.fromisoformat(today) - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT date, activity_type, distance_meters, duration_seconds, avg_hr, "
        "avg_pace_sec_per_km, aerobic_te, training_load FROM activities "
        "WHERE date >= ? AND date <= ? ORDER BY date DESC, start_time DESC",
        (cutoff, today),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        w: dict = {"date": r["date"], "type": r["activity_type"]}
        mi = units.to_miles(r["distance_meters"])
        if mi is not None:
            w["distance_mi"] = round(mi, 2)
        pace = units.format_pace_min_per_mi(r["avg_pace_sec_per_km"])
        if pace:
            w["pace_min_per_mi"] = pace
        dur = units.format_duration(r["duration_seconds"])
        if dur:
            w["duration"] = dur
        if r["aerobic_te"] is not None:
            w["aerobic_te"] = round(r["aerobic_te"], 1)
        if r["training_load"] is not None:
            w["training_load"] = round(r["training_load"], 1)
        if r["avg_hr"] is not None:
            w["avg_hr"] = r["avg_hr"]
        out.append(w)
    return out


def _continuity(recent_briefs: list[dict] | None) -> list[str]:
    """Last-7 brief headlines (continuity). Accepts a list of brief dicts."""
    if not recent_briefs:
        return []
    out: list[str] = []
    for b in recent_briefs[:7]:
        for tk in (b.get("takeaways") or [])[:1]:
            hl = tk.get("headline")
            if hl:
                out.append(hl)
    return out
