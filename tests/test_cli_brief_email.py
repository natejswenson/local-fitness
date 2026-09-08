"""Tests for `fitness brief-email` (the 19:00 launchd job's entry point).

Scope matches ``test_cli.py``: WIRING, not the downstream modules. Rendering is
covered by ``test_email_render.py`` and the MIME tree by ``test_mailer.py``;
what is under test here is the ordering and the guards — which steps run, which
are skipped, what the exit code is, and above all **when the sent marker gets
written**.

The marker is the load-bearing piece. It is the only thing standing between the
20:00 backstop and a second copy of the brief in the inbox every night, and its
correctness is asymmetric: written too eagerly and a failed 19:00 is never
retried; written too late (or never) and every success is re-sent an hour
later. Both failure modes are invisible in a unit test of any single function.
"""
from __future__ import annotations

import email
from datetime import date as Date
from email import policy
from pathlib import Path

import pytest
from click.testing import CliRunner

from local_fitness import cli, db
from local_fitness.agent import briefs, mailer
from local_fitness.agent import tools as agent_tools

BRIEF_JSON = """{
  "date": "%s",
  "user_name": "Nate",
  "generated_at": "%sT06:31:20.257259",
  "takeaways": [
    {"headline": "Long run 9mi today", "summary": "TSB +5.4.",
     "tone": "positive", "metric": null, "details": "Plan: 9mi easy-steady."}
  ]
}"""

TODAY = Date.today().isoformat()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A brief on disk, every heavy step stubbed, and a record of what ran."""
    calls: dict[str, int] = {"pull": 0, "recompute": 0, "generate": 0,
                             "send": 0, "notify": 0}
    # No DB: `brief-email` never calls init_schema, so this is what a fresh
    # clone and CI both look like. Settings must resolve from env/defaults.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "nope" / "fitness.db")
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    (briefings / f"{TODAY}.json").write_text(BRIEF_JSON % (TODAY, TODAY))
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", briefings)

    def _pull():
        calls["pull"] += 1
        return {"status": "ok", "days_pulled": 1}

    monkeypatch.setattr(cli.daily_ingest, "pull", _pull)
    monkeypatch.setattr(cli.baselines, "recompute",
                        lambda *a, **k: calls.__setitem__("recompute", calls["recompute"] + 1))
    monkeypatch.setattr(cli.briefing_mod, "generate_and_save",
                        lambda *a, **k: calls.__setitem__("generate", calls["generate"] + 1))
    monkeypatch.setattr(cli, "_notify",
                        lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1))

    async def _assemble(brief, target_date):
        return {}, None

    monkeypatch.setattr(agent_tools, "assemble_brief_render_inputs", _assemble)

    def _send(msg, cfg):
        calls["send"] += 1
        calls["last_msg"] = msg  # type: ignore[assignment]
        calls["last_cfg"] = cfg  # type: ignore[assignment]

    monkeypatch.setattr(mailer, "send", _send)
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", "pw")
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_EMAIL_TO", raising=False)

    calls["dir"] = briefings  # type: ignore[assignment]
    return calls


def marker(wired) -> Path:
    return wired["dir"] / f".emailed-{TODAY}"


# --- the happy path --------------------------------------------------------

def test_full_run_pulls_recomputes_regenerates_and_sends(runner, wired):
    result = runner.invoke(cli.main, ["brief-email"])
    assert result.exit_code == 0, result.output
    assert wired["pull"] == 1
    assert wired["recompute"] == 1
    assert wired["generate"] == 1
    assert wired["send"] == 1
    assert "Emailed Evening Brief" in result.output


def test_a_confirmed_send_writes_the_marker(runner, wired):
    assert not marker(wired).exists()
    runner.invoke(cli.main, ["brief-email"])
    assert marker(wired).exists()


def test_no_pull_skips_the_pull_but_still_sends(runner, wired):
    runner.invoke(cli.main, ["brief-email", "--no-pull"])
    assert wired["pull"] == 0
    assert wired["recompute"] == 0
    assert wired["send"] == 1


def test_no_generate_emails_the_saved_brief_without_regenerating(runner, wired):
    runner.invoke(cli.main, ["brief-email", "--no-generate"])
    assert wired["generate"] == 0
    assert wired["send"] == 1


# --- the backstop guard ----------------------------------------------------

def test_if_unsent_is_a_no_op_once_the_marker_exists(runner, wired):
    marker(wired).write_text("<already@sent>")
    result = runner.invoke(cli.main, ["brief-email", "--if-unsent"])
    assert result.exit_code == 0
    assert "already emailed" in result.output
    # The whole point: the 20:00 fire must not re-pull, re-generate OR re-send.
    assert (wired["pull"], wired["generate"], wired["send"]) == (0, 0, 0)


def test_if_unsent_runs_normally_when_the_marker_is_absent(runner, wired):
    result = runner.invoke(cli.main, ["brief-email", "--if-unsent"])
    assert result.exit_code == 0
    assert wired["send"] == 1


def test_a_failed_send_leaves_no_marker_so_the_backstop_retries(runner, wired, monkeypatch):
    def _boom(msg, cfg):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "send", _boom)
    result = runner.invoke(cli.main, ["brief-email"])

    assert result.exit_code != 0
    # Writing the marker before/regardless of the send would silently cost the
    # day's email: 20:00 would see it and skip.
    assert not marker(wired).exists()
    assert wired["notify"] == 1  # silence must not be the only signal


def test_a_failed_generation_notifies_and_exits_nonzero(runner, wired, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("SDK stream died")

    monkeypatch.setattr(cli.briefing_mod, "generate_and_save", _boom)
    result = runner.invoke(cli.main, ["brief-email"])

    assert result.exit_code != 0
    assert wired["send"] == 0
    assert wired["notify"] == 1
    assert not marker(wired).exists()


# --- the conversational kill switch ----------------------------------------

def test_disabled_skips_the_whole_job_not_just_the_send(runner, wired, monkeypatch):
    # A kill switch that still spends a Garmin pull and an LLM run every night
    # and throws the result away is not a kill switch.
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "false")
    result = runner.invoke(cli.main, ["brief-email"])

    assert result.exit_code == 0
    assert "disabled" in result.output
    assert (wired["pull"], wired["generate"], wired["send"]) == (0, 0, 0)
    assert not marker(wired).exists()


def test_re_enabling_resumes_the_send(runner, wired, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "true")
    runner.invoke(cli.main, ["brief-email"])
    assert wired["send"] == 1


def test_dry_run_still_works_while_disabled(runner, wired, monkeypatch, tmp_path):
    # Inspecting a disabled setup must stay possible — that is how you check
    # what it WOULD send before turning it back on.
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "false")
    out = tmp_path / "b.eml"
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate",
                                      "--dry-run", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert wired["send"] == 0


# --- guards ----------------------------------------------------------------

def test_missing_brief_exits_1_without_sending(runner, wired):
    (wired["dir"] / f"{TODAY}.json").unlink()
    result = runner.invoke(cli.main, ["brief-email", "--no-generate"])
    assert result.exit_code == 1
    assert "nothing to email" in result.output
    assert wired["send"] == 0


def test_unconfigured_mail_exits_2_naming_the_variable(runner, wired, monkeypatch):
    # Distinct from a send failure: actionable, and never worth retrying.
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_PASSWORD")
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    assert result.exit_code == 2
    assert "LOCAL_FITNESS_SMTP_PASSWORD" in result.output
    assert wired["send"] == 0
    assert not marker(wired).exists()


def test_an_explicit_date_selects_that_brief(runner, wired):
    (wired["dir"] / "2026-08-01.json").write_text(BRIEF_JSON % ("2026-08-01", "2026-08-01"))
    result = runner.invoke(
        cli.main, ["brief-email", "--date", "2026-08-01", "--no-pull", "--no-generate"])
    assert result.exit_code == 0
    assert "2026-08-01" in result.output
    # The marker is per-date, so yesterday's send must not suppress today's.
    assert (wired["dir"] / ".emailed-2026-08-01").exists()
    assert not marker(wired).exists()


# --- dry run ---------------------------------------------------------------

def test_dry_run_writes_the_eml_and_opens_no_socket(runner, wired, tmp_path):
    out = tmp_path / "out" / "brief.eml"
    result = runner.invoke(
        cli.main, ["brief-email", "--no-pull", "--no-generate", "--dry-run", str(out)])

    assert result.exit_code == 0
    assert wired["send"] == 0
    assert out.exists()
    parsed = email.message_from_bytes(out.read_bytes(), policy=policy.default)
    assert parsed["Subject"] == f"Evening Brief · {TODAY}"
    assert "Nothing was sent." in result.output


def test_dry_run_needs_no_credentials(runner, wired, tmp_path, monkeypatch):
    # The reason dry-run exists: check the composed message BEFORE setting up
    # an app password, which is exactly when load_config refuses.
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_PASSWORD")
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_USER")
    out = tmp_path / "brief.eml"
    result = runner.invoke(
        cli.main, ["brief-email", "--no-pull", "--no-generate", "--dry-run", str(out)])
    assert result.exit_code == 0
    assert out.exists()


def test_dry_run_never_writes_the_sent_marker(runner, wired, tmp_path):
    # A dry run that marked the date as sent would suppress the real 19:00 send.
    runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate",
                             "--dry-run", str(tmp_path / "b.eml")])
    assert not marker(wired).exists()


def test_to_override_reaches_the_delivered_message(runner, wired, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_TO", "default@x.com")
    result = runner.invoke(
        cli.main, ["brief-email", "--no-pull", "--no-generate",
                   "--to", "someone@else.com"])

    assert result.exit_code == 0
    assert wired["last_cfg"].to == ("someone@else.com",)
    assert wired["last_msg"]["To"] == "someone@else.com"
    assert "someone@else.com" in result.output


def test_the_delivered_message_carries_the_rendered_brief(runner, wired):
    # Guards the seam between rendering and delivery: the command must hand
    # `mailer` the composed brief, not an empty or placeholder body.
    runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    msg = wired["last_msg"]
    assert msg["Subject"] == f"Evening Brief · {TODAY}"
    assert "Long run 9mi today" in msg.get_body(preferencelist=("html",)).get_content()
    assert "LONG RUN 9MI TODAY" in msg.get_body(preferencelist=("plain",)).get_content()


# --- #241: which coaching line shipped -------------------------------------
#
# The substitution was already logged at WARNING and ran unnoticed for 23 of 30
# nights, because that WARNING lands in a 154 KB launchd ERROR log nobody
# scans. So these assert the two channels a human actually meets: stdout (the
# 4 KB one-line-a-night out log) and the nightly macOS notification. Asserting
# on stderr or a log record would re-test exactly what already failed.

PLAN_SECTION = {
    "adherence_pct": 83,
    "sessions_adherence_pct": 83,
    "rest_days_counted": 1,
    "goal_type": "10k",
    "days_to_race": 42,
    "week_planned_mi": 36.0,
    "week_actual_mi": 14.2,
    "week_walk_mi": 6.0,
    "slips": 2,
    "today": {
        "type": "long",
        "distance_mi": 9.0,
        "pace_min_per_mi": "9:23",
        "description": "Long run 9mi @ easy-steady.",
        "coaching_line": "Yesterday you hit the session clean.",
        "coaching_line_source": "generated",
    },
    "last_7_days": [
        {"date": "2026-08-07", "type": "long", "planned_mi": 9.0,
         "actual_mi": None, "verdict": "scheduled"},
    ],
}


def _with_plan_section(monkeypatch, wired, source: str | None):
    """Re-stub the assemble call with a REAL plan section whose today dict
    carries ``source`` (or no source key at all when None), and record the
    notification text the run produced."""
    section = {**PLAN_SECTION, "today": dict(PLAN_SECTION["today"])}
    if source is None:
        section["today"].pop("coaching_line_source")
    else:
        section["today"]["coaching_line_source"] = source

    async def _assemble(brief, target_date):
        return {}, section

    monkeypatch.setattr(agent_tools, "assemble_brief_render_inputs", _assemble)
    notes: list[str] = []
    monkeypatch.setattr(cli, "_notify", lambda msg: notes.append(msg))
    return notes


def test_a_generated_coaching_line_is_named_on_stdout(runner, wired, monkeypatch):
    notes = _with_plan_section(monkeypatch, wired, "generated")
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    assert result.exit_code == 0, result.output
    # stdout specifically — the out log is the file that gets read.
    assert "Coaching line: generated" in result.stdout
    assert notes == ["Evening brief emailed"]


def test_a_template_coaching_line_is_named_on_stdout_and_in_the_notification(
    runner, wired, monkeypatch
):
    notes = _with_plan_section(monkeypatch, wired, "fallback")
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    assert result.exit_code == 0, result.output
    assert "Coaching line: fallback" in result.stdout
    # One notification either way, and this one says the coach never spoke.
    assert notes == ["Evening brief emailed (coaching line: TEMPLATE)"]
    assert wired["send"] == 1  # a template line is not a failure


def test_the_source_line_prints_on_a_dry_run_too(runner, wired, monkeypatch, tmp_path):
    # --dry-run returns before the send, so a check that only ran after it
    # would never fire on the path used to verify the job by hand.
    _with_plan_section(monkeypatch, wired, "fallback")
    result = runner.invoke(
        cli.main, ["brief-email", "--no-pull", "--no-generate",
                   "--dry-run", str(tmp_path / "b.eml")])
    assert result.exit_code == 0, result.output
    assert "Coaching line: fallback" in result.stdout


def test_no_plan_section_prints_nothing_and_still_sends(runner, wired):
    # The fixture's own stub returns ({}, None) — a brief with no active plan.
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    assert result.exit_code == 0, result.output
    assert "Coaching line:" not in result.stdout
    assert wired["send"] == 1


def test_an_older_payload_without_the_source_key_degrades_quietly(
    runner, wired, monkeypatch
):
    notes = _with_plan_section(monkeypatch, wired, None)
    result = runner.invoke(cli.main, ["brief-email", "--no-pull", "--no-generate"])
    assert result.exit_code == 0, result.output
    assert "Coaching line:" not in result.stdout
    assert notes == ["Evening brief emailed"]
