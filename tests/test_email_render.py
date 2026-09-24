"""Behavioral budget, source safety and email-client compatibility checks."""
from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser

import pytest

from local_fitness.agent import email_digest, email_render
from local_fitness.agent.branding import DEFAULT_THEME
from local_fitness.agent.email_digest import EmailInputs
from local_fitness.agent.schemas import Brief


def make_brief(**over):
    payload = dict(date="2026-08-07", user_name="Example",
                   generated_at="2026-08-07T19:00:00", takeaways=[dict(
                       headline="A steady day of movement.", summary="Your walk added useful time on your feet.",
                       tone="positive", details="PRIVATE LONG DETAILS " * 100)])
    payload.update(over)
    return Brief.model_validate(payload)


def make_inputs(**over):
    payload = dict(daily={"steps": 10240, "sleep_score": 86, "rhr": 52},
        workout_score=4.25, score_saved=True, activities=[{
        "activity_type": "treadmill_running", "distance_meters": 6759.2448,
        "duration_seconds": 4200, "avg_pace_sec_per_km": 650}], has_plan=True,
        tomorrow=[dict(type="easy", target_distance_m=4828.032,
                       target_pace_sec_per_km=390, description="Keep it relaxed.")],
        synced_at="2026-08-07T18:52:00")
    payload.update(over)
    return EmailInputs(**payload)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.words = []

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "div"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"head", "div"}:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.words.extend(data.split())


def render(brief=None, inputs=None, theme=None):
    digest = email_digest.compose(brief or make_brief(), inputs or make_inputs())
    return digest, email_render.build_html(digest, theme or DEFAULT_THEME)


def test_email_is_a_digest_with_matching_text_and_no_details_or_assets():
    digest, html = render()
    text = email_render.build_text(digest)
    visible = VisibleText()
    visible.feed(html)
    assert visible.words == text.split()
    assert len(visible.words) <= 80
    assert "4.2 mi walked." in text
    for metric in ("10,240 Steps so far", "1h 10m Workout time",
                   "4.25/5 Main workout score (saved)", "86/100 Sleep score", "52 bpm Resting HR"):
        assert metric in text
    assert "Tomorrow · Aug 08" in text and "Easy run" in text
    assert "Full instructions in your fitness chat." in text
    for omitted in ("PRIVATE LONG DETAILS", "<img", "<style", "@font-face", "data:",
                    "cid:", "display:flex", "display:grid", "linkedin", "Adherence"):
        assert omitted not in html
    assert 'role="presentation"' in html and '<html lang="en">' in html


def test_subject_still_separates_each_evening():
    assert email_render.subject_for(make_brief()) == "Evening Brief · 2026-08-07"
    assert email_render.subject_for(make_brief(date="2026-12-25")).endswith("2026-12-25")
    assert email_render.chart_cid(3) == "chart3"


def test_critical_takeaway_wins_and_cannot_hide_behind_short_positive_prose():
    brief = make_brief()
    critical = brief.takeaways[0].model_copy(update=dict(
        headline="Important context " * 100, tone="critical"))
    brief.takeaways.append(critical)
    digest, html = render(brief)
    assert digest.insight == email_digest.BRIEF_CUE
    assert "A steady day" not in html
    assert html.count(DEFAULT_THEME["colors"]["accent"]) == 1


@pytest.mark.parametrize("prose", ["<script>alert(1)</script>", "**bold**", "x" * 300,
                                   "word " * 300, "[click](https://example.com)"])
def test_generated_markup_and_overlong_prose_degrade_without_truncating_advice(prose):
    brief = make_brief()
    brief.takeaways[0].summary = prose
    digest, html = render(brief)
    assert digest.insight == email_digest.BRIEF_CUE
    assert prose not in html
    assert len(email_render.build_text(digest).split()) <= 80


def test_all_html_boundary_strings_are_escaped():
    digest, _ = render()
    digest = replace(digest, headline='<img src=x onerror="bad"> & stuff')
    html = email_render.build_html(digest, DEFAULT_THEME)
    assert '<img src=x' not in html
    assert '&lt;img src=x onerror=&quot;bad&quot;&gt; &amp; stuff' in html


def test_theme_override_reaches_the_email_without_a_social_byline():
    theme = {**DEFAULT_THEME, "colors": {**DEFAULT_THEME["colors"], "paper": "custom-paper"}}
    _, html = render(theme=theme)
    assert "background:custom-paper" in html
    assert DEFAULT_THEME["identity"]["byline"] not in html


@pytest.mark.parametrize("count", [1, 2, 3, 50])
def test_total_budget_includes_multiple_complete_session_outlines(count):
    workouts = [dict(type="interval", target_distance_m=10000, target_duration_sec=3600,
                     target_pace_sec_per_km=300, target_hr_max=155,
                     description="3x1mi with 2min recovery. " * 100)] * count
    digest, html = render(inputs=make_inputs(tomorrow=workouts))
    text = email_render.build_text(digest)
    visible = VisibleText()
    visible.feed(html)
    assert len(visible.words) <= 80
    assert visible.words == text.split()
    assert email_digest.FULL_SESSION_CUE in text
    if len(digest.tomorrow) == count and count <= 2:
        for line in digest.tomorrow:
            assert "6.2 mi" in line and "1h 00m" in line
            assert "8:03/mi" in line and "HR ≤155" in line
    else:
        assert f"{count} sessions planned." in text


def test_brief_time_is_separate_from_sync_time():
    digest, _ = render(make_brief(generated_at="2026-08-07T06:31:00"))
    assert digest.insight_label == "Brief · Aug 07 06:31"
    assert digest.provenance == "Synced Aug 07 18:52"
    assert email_digest.compose(make_brief(generated_at="invalid"), EmailInputs()).insight_label == "Saved brief"
    assert email_digest.compose(make_brief(generated_at=None), EmailInputs()).insight_label == "Saved brief"


@pytest.mark.parametrize("rows,expected", [
    (None, "Activity unavailable."),
    ([], "No workouts logged."),
    ([dict(activity_type="cycling", distance_meters=20000, avg_pace_sec_per_km=100)], "1 workout logged."),
    ([dict(activity_type="strength_training", duration_seconds=1800)], "1 workout logged."),
    ([dict(activity_type="running", distance_meters=1609.344)], "1.0 mi on foot."),
    ([dict(activity_type="running", distance_meters=1609.344, avg_pace_sec_per_km=300)], "1.0 mi run."),
    ([dict(activity_type="running", distance_meters=1609.344, avg_pace_sec_per_km=300),
      dict(activity_type="cycling", distance_meters=20000, avg_pace_sec_per_km=100)], "2 workouts logged."),
    ([dict(activity_type="running", distance_meters=None),
      dict(activity_type="running", distance_meters=1609.344)], "2 workouts logged."),
])
def test_activity_is_measured_and_missing_totals_never_look_complete(rows, expected):
    digest = email_digest.compose(make_brief(), make_inputs(activities=rows))
    assert digest.headline == expected
    expected_duration = ("0m" if rows == [] else "30m"
                         if rows == [dict(activity_type="strength_training", duration_seconds=1800)]
                         else "—")
    assert digest.stats[1][0] == expected_duration


def test_mixed_running_walking_is_named_and_cycling_never_added_to_foot_miles():
    rows = [dict(activity_type="running", distance_meters=1609.344, avg_pace_sec_per_km=300),
            dict(activity_type="treadmill_running", distance_meters=3218.688, avg_pace_sec_per_km=700)]
    digest = email_digest.compose(make_brief(), make_inputs(activities=rows))
    assert digest.headline == "3.0 mi on foot."
    assert digest.activity_note == "1.0 mi run · 2.0 mi walked"


def test_missing_duration_is_not_summed_as_zero_and_zero_steps_are_real():
    rows = [dict(activity_type="cycling", duration_seconds=1800), dict(activity_type="strength")]
    digest = email_digest.compose(make_brief(), make_inputs(
        activities=rows, daily={"steps": 0, "sleep_score": 0}, workout_score=None, score_saved=False))
    assert digest.stats == (("0", "Steps so far"), ("—", "Workout time"),
                            ("—", "Main workout score"), ("0/100", "Sleep score"), ("—", "Resting HR"))


@pytest.mark.parametrize("inputs,expected", [
    (EmailInputs(), "Plan unavailable."),
    (EmailInputs(tomorrow=[], has_plan=False), "No active plan."),
    (EmailInputs(tomorrow=[], has_plan=True), "No session scheduled."),
    (EmailInputs(tomorrow=[{"type": "rest", "target_distance_m": 5000}], has_plan=True), "Rest"),
])
def test_rest_missing_plan_and_no_prescription_are_different(inputs, expected):
    digest = email_digest.compose(make_brief(date="2025-12-31"), inputs)
    assert digest.tomorrow == (expected,)
    assert digest.tomorrow_label == "Tomorrow · Jan 01"


def test_prescribed_walk_and_km_preferences_apply_to_activity_and_plan():
    inputs = make_inputs(distance_unit="km", tomorrow=[dict(type="easy", target_distance_m=5000,
                         target_pace_sec_per_km=650)], has_plan=True)
    digest = email_digest.compose(make_brief(), inputs)
    assert digest.headline == "6.8 km walked."
    assert digest.tomorrow == ("Walk · 5.0 km · @ 10:50/km",)


def test_unknown_legacy_plan_type_cannot_inject_unbounded_text():
    digest = email_digest.compose(make_brief(), make_inputs(tomorrow=[{"type": "bad " * 500}]))
    assert digest.tomorrow == ("Workout",)
    assert len(email_render.build_text(digest).split()) <= 80


@pytest.fixture
def email_db(tmp_path):
    from local_fitness import db
    path = tmp_path / "fitness.db"
    db.init_schema(path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO daily_metrics(date,steps,sleep_score,rhr) VALUES ('2025-12-31',12345,88,54)")
        conn.execute("INSERT INTO activities(activity_id,date,activity_type,distance_meters,duration_seconds,avg_pace_sec_per_km) VALUES (1,'2025-12-31','treadmill_running',3218.688,2100,650)")
        conn.execute("INSERT INTO activities(activity_id,date,activity_type,distance_meters) VALUES (2,'2026-01-01','running',99999)")
        conn.execute("INSERT INTO training_plans(plan_id,status,goal_type,race_date,created_at) VALUES (1,'active','5k','2027-02-01','2026-12-01')")
        for seq, kind in [(2, "cross"), (1, "easy")]:
            conn.execute("INSERT INTO plan_workouts(plan_id,date,seq,week_index,type,target_duration_sec,description) VALUES (1,'2026-01-01',?,1,?,1800,'Full instructions.')", (seq, kind))
    return path


def test_readonly_loader_selects_target_day_and_all_ordered_tomorrow_sessions(email_db):
    from local_fitness import db
    with db.connect_readonly(email_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    inputs = email_digest.load_inputs("2025-12-31", email_db)
    assert inputs.daily == {"steps": 12345, "sleep_score": 88, "rhr": 54}
    assert len(inputs.activities) == 1 and inputs.activities[0]["distance_meters"] == 3218.688
    assert [w["type"] for w in inputs.tomorrow] == ["easy", "cross"]
    assert inputs.historical is True and inputs.synced_at is None
    digest = email_digest.compose(make_brief(date="2025-12-31"), inputs)
    assert digest.provenance == "Archived day · current saved plan"
    with db.connect_readonly(email_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == before


def test_freshness_only_uses_successful_sync_covering_current_date(email_db):
    from datetime import date

    from local_fitness import db
    today = date.today().isoformat()
    with db.connect(email_db) as conn:
        conn.execute("INSERT INTO ingest_runs(started_at,completed_at,status,last_date_fetched) VALUES (?,?,?,?)",
                     (today + "T18:00:00", today + "T18:05:00", "success", today))
        conn.execute("INSERT INTO ingest_runs(started_at,completed_at,status,last_date_fetched) VALUES (?,?,?,?)",
                     (today + "T19:00:00", today + "T19:05:00", "failure", today))
    assert email_digest.load_inputs(today, email_db).synced_at == today + "T18:05:00"


def test_missing_database_and_missing_tables_fail_soft_without_creating_files(tmp_path):
    import sqlite3

    missing = tmp_path / "missing" / "fitness.db"
    inputs = email_digest.load_inputs("2026-08-07", missing)
    assert inputs.activities is None and inputs.tomorrow is None
    assert not missing.parent.exists()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    inputs = email_digest.load_inputs("2026-08-07", empty)
    assert inputs.activities is None and inputs.tomorrow is None


def test_real_cli_dry_run_reads_digest_facts_and_writes_only_mime(email_db, tmp_path, monkeypatch):
    from email import message_from_bytes, policy

    from click.testing import CliRunner

    from local_fitness import cli, db
    from local_fitness.agent import briefs

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", email_db)
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", tmp_path)
    (tmp_path / "2025-12-31.json").write_text(make_brief(date="2025-12-31").model_dump_json())
    out = tmp_path / "digest.eml"
    result = CliRunner().invoke(cli.main, ["brief-email", "--date", "2025-12-31", "--no-pull", "--no-generate", "--dry-run", str(out)])
    assert result.exit_code == 0, result.output
    parsed = message_from_bytes(out.read_bytes(), policy=policy.default)
    text = parsed.get_body(preferencelist=("plain",)).get_content()
    assert "2.0 mi walked." in text and "12,345" in text
    assert "Tomorrow · Jan 01" in text
    assert "Cross" in text and "Easy" in text
    assert len(text.split()) <= 80
    assert not list(tmp_path.glob(".emailed-*"))
    assert [p.get_content_type() for p in parsed.walk()] == ["multipart/alternative", "text/plain", "text/html"]


def test_critical_context_survives_the_total_budget_on_a_dense_double_day():
    brief = make_brief()
    brief.takeaways[0].tone = "critical"
    workouts = [dict(type="interval", target_distance_m=10000, target_duration_sec=3600,
                     target_pace_sec_per_km=300, target_hr_max=155)] * 2
    digest = email_digest.compose(brief, make_inputs(tomorrow=workouts))
    assert digest.critical is True
    assert digest.insight == email_digest.BRIEF_CUE
    assert digest.tomorrow == ("2 sessions planned.",)
    assert len(email_render.build_text(digest).split()) <= 80


def test_email_input_loading_does_not_import_the_agent_tool_runtime(email_db):
    import subprocess
    import sys
    from datetime import date

    from local_fitness import db

    today = date.today().isoformat()
    with db.connect(email_db) as conn:
        conn.execute("UPDATE activities SET date=? WHERE activity_id=1", (today,))

    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from pathlib import Path; "
        "from local_fitness.agent.email_digest import load_inputs; "
        "load_inputs(sys.argv[2], Path(sys.argv[1])); "
        "assert 'local_fitness.agent.tools' not in sys.modules; "
        "assert 'claude_agent_sdk' not in sys.modules",
        str(email_db), today,
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_main_score_prefers_saved_capped_rating_for_longest_run_not_bike_or_walk(email_db, monkeypatch):
    from local_fitness import db
    from local_fitness.agent import report_card

    with db.connect(email_db) as conn:
        for aid, kind, distance, pace in [(3, "cycling", 40000, 100),
                                          (4, "running", 5000, 330), (5, "running", 1000, 280)]:
            conn.execute("INSERT INTO activities(activity_id,date,activity_type,distance_meters,duration_seconds,avg_pace_sec_per_km) VALUES (?,'2025-12-31',?,?,1800,?)",
                         (aid, kind, distance, pace))
        conn.execute("INSERT INTO report_cards(activity_id,activity_date,graded_at,overall_stars,mean_stars,card_json) VALUES (4,'2025-12-31','2025-12-31T19:00:00',2.75,4.5,'{}')")
        before = list(conn.iterdump())
    monkeypatch.setattr(report_card, "build_card", lambda *a, **kw: pytest.fail("saved score must not be recomputed"))
    inputs = email_digest.load_inputs("2025-12-31", email_db)
    assert inputs.workout_score == 2.75 and inputs.score_saved is True
    with db.connect_readonly(email_db) as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("target", ["2025-12-31", "2099-01-01"])
def test_missing_historical_or_future_score_is_never_recomputed(email_db, monkeypatch, target):
    from local_fitness import db
    from local_fitness.agent import report_card

    with db.connect(email_db) as conn:
        conn.execute("UPDATE activities SET date=? WHERE activity_id=1", (target,))
        # A saved rating for another date is not evidence for the target date.
        conn.execute("INSERT INTO report_cards(activity_id,activity_date,graded_at,overall_stars,card_json) VALUES (1,'2025-01-01','2025-01-01',4.5,'{}')")
    monkeypatch.setattr(report_card, "load_report_card_inputs", lambda *a, **kw: pytest.fail("must not regrade"))
    assert email_digest.load_inputs(target, email_db).workout_score is None


def test_current_day_score_uses_real_local_grader_without_writes(email_db, monkeypatch):
    from datetime import date

    from local_fitness import db
    from local_fitness.agent import report_card
    from local_fitness.ingest import details

    today = date.today().isoformat()
    with db.connect(email_db) as conn:
        conn.execute("UPDATE activities SET date=?,avg_hr=180,avg_pace_sec_per_km=330,duration_seconds=1062 WHERE activity_id=1", (today,))
        conn.execute("INSERT INTO plan_workouts(plan_id,date,seq,week_index,type,target_distance_m,target_pace_sec_per_km,target_hr_max,description) VALUES (1,?,1,1,'easy',3218.688,330,130,'Easy session.')", (today,))
        before = list(conn.iterdump())
    monkeypatch.setattr(details, "get_hr_samples", lambda *a, **kw: pytest.fail("no HR trace/network access"))
    with db.connect_readonly(email_db) as conn:
        data = report_card.load_report_card_inputs(conn, activity_id=1, hr_trace=False)
        data.pop("other_activities_on_date")
        expected = report_card.build_card(**data)["overall"]
    inputs = email_digest.load_inputs(today, email_db)
    assert inputs.workout_score == expected["stars"]
    assert inputs.workout_score < expected["mean_stars"]
    assert inputs.score_saved is False
    with db.connect_readonly(email_db) as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("score", [None, 0, 6, float("inf")])
def test_unavailable_or_invalid_saved_scores_never_become_a_rating(email_db, score):
    from local_fitness import db

    with db.connect(email_db) as conn:
        conn.execute("INSERT INTO report_cards(activity_id,activity_date,graded_at,overall_stars,card_json) VALUES (1,'2025-12-31','2025-12-31',?,'{}')", (score,))
    inputs = email_digest.load_inputs("2025-12-31", email_db)
    assert inputs.workout_score is None and inputs.score_saved is False


def test_score_storage_failure_preserves_other_four_metrics_and_plan(email_db):
    from local_fitness import db

    with db.connect(email_db) as conn:
        conn.execute("DROP TABLE report_cards")
    digest = email_digest.compose(make_brief(date="2025-12-31"),
                                 email_digest.load_inputs("2025-12-31", email_db))
    assert [value for value, _ in digest.stats] == ["12,345", "35m", "—", "88/100", "54 bpm"]
    assert len(digest.tomorrow) == 2


def test_current_workout_with_no_gradeable_reference_has_no_score(email_db):
    from datetime import date

    from local_fitness import db

    today = date.today().isoformat()
    with db.connect(email_db) as conn:
        conn.execute("UPDATE activities SET date=? WHERE activity_id=1", (today,))
    inputs = email_digest.load_inputs(today, email_db)
    assert inputs.workout_score is None and inputs.score_saved is False
    assert len(inputs.activities) == 1
