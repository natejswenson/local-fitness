"""Tests for the `fitness` CLI (src/local_fitness/cli.py).

Scope: WIRING, not the downstream modules. We use click's CliRunner and
monkeypatch the heavy targets (`daily_ingest.pull`, `backfill_mod.backfill`,
`baselines.recompute`, `briefing_mod.generate_and_save`, `subprocess.run`) so
that what's under test is arg parsing, the `name → user_name` alias, exit-code
paths, date parsing, and output formatting. A real tmp SQLite backs the
commands that actually read/write the DB (`config`, `status`).

Deliberately NOT covered: `setup` (interactive Keychain/prompt flow) and
`serve` (boots uvicorn) — not worth harnessing.
"""
from __future__ import annotations

from datetime import date as Date
from pathlib import Path

import pytest
from click.testing import CliRunner

from local_fitness import cli, db


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def dbp(tmp_path, monkeypatch):
    """Point the db module at a fresh tmp SQLite so config/status touch real
    tables but never the developer's data/fitness.db."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


# --------------------------------------------------------------------------
# config set — the `name → user_name` alias (real logic worth covering)
# --------------------------------------------------------------------------

def test_config_set_name_alias_maps_to_user_name(runner, dbp):
    # Real logic: `name` is rewritten to `user_name` before the DB write.
    result = runner.invoke(cli.main, ["config", "set", "name", "Nate"])
    assert result.exit_code == 0
    assert "user_name = Nate" in result.output
    assert db.get_setting("user_name") == "Nate"
    # The literal key `name` must NOT exist — only the alias target.
    assert db.get_setting("name") is None


def test_config_set_passthrough_key(runner, dbp):
    # Non-aliased keys are stored verbatim.
    result = runner.invoke(cli.main, ["config", "set", "riegel_lookback_days", "200"])
    assert result.exit_code == 0
    assert db.get_setting("riegel_lookback_days") == "200"


# --------------------------------------------------------------------------
# config get — alias + unset + all-settings formatting (real logic)
# --------------------------------------------------------------------------

def test_config_get_name_alias(runner, dbp):
    db.set_setting("user_name", "Nate")
    result = runner.invoke(cli.main, ["config", "get", "name"])
    assert result.exit_code == 0
    assert result.output.strip() == "Nate"


def test_config_get_unset_key(runner, dbp):
    result = runner.invoke(cli.main, ["config", "get", "does_not_exist"])
    assert result.exit_code == 0
    assert result.output.strip() == "(unset)"


def test_config_get_all_empty(runner, dbp):
    result = runner.invoke(cli.main, ["config", "get"])
    assert result.exit_code == 0
    assert "(no settings configured)" in result.output


def test_config_get_all_lists_settings(runner, dbp):
    db.set_setting("user_name", "Nate")
    db.set_setting("riegel_lookback_days", "200")
    result = runner.invoke(cli.main, ["config", "get"])
    assert result.exit_code == 0
    assert "user_name = Nate" in result.output
    assert "riegel_lookback_days = 200" in result.output


# --------------------------------------------------------------------------
# status — DB stats + last-run formatting (real DB read + formatting)
# --------------------------------------------------------------------------

def test_status_empty_db(runner, dbp):
    result = runner.invoke(cli.main, ["status"])
    assert result.exit_code == 0
    # Row-count table renders every tracked table, all at zero.
    for table in ("daily_metrics", "activities", "baselines",
                  "body_battery_samples", "stress_samples"):
        assert table in result.output
    assert "No ingest runs yet." in result.output


def test_status_reports_last_ingest_run(runner, dbp):
    with db.connect(dbp) as conn:
        conn.execute(
            "INSERT INTO ingest_runs (started_at, status, source) VALUES (?, ?, ?)",
            ("2026-06-25T06:30:00", "success", "daily"),
        )
    result = runner.invoke(cli.main, ["status"])
    assert result.exit_code == 0
    assert "Last ingest run:" in result.output
    assert "success" in result.output


# --------------------------------------------------------------------------
# pull — date parsing, output line, and the sys.exit(1) error path
# --------------------------------------------------------------------------

def _ok_pull_result(**overrides):
    base = {
        "status": "success",
        "days_pulled": 3,
        "activities_loaded": 2,
        "last_date": "2026-06-25",
        "error": None,
    }
    base.update(overrides)
    return base


def test_pull_happy_path_formats_status_line(runner, monkeypatch):
    captured = {}

    def fake_pull(*, through, force_from, mfa_callback):
        captured["through"] = through
        captured["force_from"] = force_from
        return _ok_pull_result()

    monkeypatch.setattr(cli.daily_ingest, "pull", fake_pull)
    result = runner.invoke(cli.main, ["pull"])
    assert result.exit_code == 0
    # No --from/--through → both None passed through.
    assert captured["through"] is None
    assert captured["force_from"] is None
    assert "Status: success" in result.output
    assert "days: 3" in result.output
    assert "activities: 2" in result.output
    assert "last: 2026-06-25" in result.output


def test_pull_parses_date_options(runner, monkeypatch):
    # Real logic: Date.fromisoformat() conversion of --from / --through strings.
    captured = {}

    def fake_pull(*, through, force_from, mfa_callback):
        captured["through"] = through
        captured["force_from"] = force_from
        return _ok_pull_result()

    monkeypatch.setattr(cli.daily_ingest, "pull", fake_pull)
    result = runner.invoke(
        cli.main, ["pull", "--from", "2026-01-01", "--through", "2026-02-01"]
    )
    assert result.exit_code == 0
    assert captured["force_from"] == Date(2026, 1, 1)
    assert captured["through"] == Date(2026, 2, 1)


def test_pull_invalid_date_raises_nonzero(runner, monkeypatch):
    # Bad ISO string → Date.fromisoformat raises before pull is ever called.
    monkeypatch.setattr(
        cli.daily_ingest, "pull",
        lambda **_: pytest.fail("pull should not run on a bad date"),
    )
    result = runner.invoke(cli.main, ["pull", "--from", "not-a-date"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)


def test_pull_error_result_exits_1(runner, monkeypatch):
    # Real logic: a result carrying an 'error' triggers sys.exit(1).
    monkeypatch.setattr(
        cli.daily_ingest, "pull",
        lambda **_: _ok_pull_result(status="error", error="garmin 503"),
    )
    result = runner.invoke(cli.main, ["pull"])
    assert result.exit_code == 1
    assert "garmin 503" in result.output


# --------------------------------------------------------------------------
# backfill — arg parsing (exists=True) + count formatting (wiring)
# --------------------------------------------------------------------------

def test_backfill_happy_path(runner, monkeypatch, tmp_path):
    zip_path = tmp_path / "export.zip"
    zip_path.write_text("not really a zip, just needs to exist")
    captured = {}

    def _fake_backfill(p):
        captured["path"] = p
        return {"activities": 12, "daily_metrics": 30}

    monkeypatch.setattr(cli.backfill_mod, "backfill", _fake_backfill)
    result = runner.invoke(cli.main, ["backfill", str(zip_path)])
    assert result.exit_code == 0
    assert "Backfill complete:" in result.output
    assert "activities: 12" in result.output
    assert "daily_metrics: 30" in result.output
    # The resolved zip Path (click.Path(path_type=Path)) is wired through.
    assert captured["path"] == zip_path
    assert isinstance(captured["path"], Path)


def test_backfill_missing_file_exit_2(runner):
    # click.Path(exists=True) rejects a nonexistent path with usage error (2).
    result = runner.invoke(cli.main, ["backfill", "/no/such/file.zip"])
    assert result.exit_code == 2


# --------------------------------------------------------------------------
# recompute-baselines — option passthrough + output (wiring)
# --------------------------------------------------------------------------

def test_recompute_baselines_default_and_output(runner, monkeypatch):
    captured = {}

    def fake_recompute(*, lookback_days):
        captured["lookback_days"] = lookback_days
        return 42

    monkeypatch.setattr(cli.baselines, "recompute", fake_recompute)
    result = runner.invoke(cli.main, ["recompute-baselines"])
    assert result.exit_code == 0
    assert captured["lookback_days"] == 90  # default
    assert "Recomputed baselines for 42 dates." in result.output


def test_recompute_baselines_custom_lookback(runner, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli.baselines, "recompute",
        lambda *, lookback_days: captured.setdefault("lb", lookback_days) or 1,
    )
    result = runner.invoke(cli.main, ["recompute-baselines", "--lookback", "30"])
    assert result.exit_code == 0
    assert captured["lb"] == 30


# --------------------------------------------------------------------------
# brief — pull/no-pull wiring, model selection, notify suppression
# --------------------------------------------------------------------------

def test_brief_default_pulls_and_uses_sonnet(runner, monkeypatch):
    calls = {"pull": 0}
    captured = {}

    monkeypatch.setattr(
        cli.daily_ingest, "pull",
        lambda *a, **k: calls.__setitem__("pull", calls["pull"] + 1) or _ok_pull_result(),
    )
    monkeypatch.setattr(cli.baselines, "recompute", lambda *a, **k: 0)
    monkeypatch.setattr(
        cli.briefing_mod, "generate_and_save",
        lambda *, model: captured.setdefault("model", model) or Path("/tmp/brief.md"),
    )
    # --no-notify so we never shell out to osascript.
    result = runner.invoke(cli.main, ["brief", "--no-notify"])
    assert result.exit_code == 0
    assert calls["pull"] == 1
    assert captured["model"] == cli.SONNET
    assert "Brief written to:" in result.output


def test_brief_no_pull_skips_pull(runner, monkeypatch):
    monkeypatch.setattr(
        cli.daily_ingest, "pull",
        lambda *a, **k: pytest.fail("pull must be skipped with --no-pull"),
    )
    monkeypatch.setattr(cli.baselines, "recompute", lambda *a, **k: 0)
    monkeypatch.setattr(
        cli.briefing_mod, "generate_and_save",
        lambda *, model: Path("/tmp/brief.md"),
    )
    result = runner.invoke(cli.main, ["brief", "--no-pull", "--no-notify"])
    assert result.exit_code == 0
    assert "Brief written to:" in result.output


def test_brief_opus_flag_selects_opus(runner, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.daily_ingest, "pull", lambda *a, **k: _ok_pull_result())
    monkeypatch.setattr(cli.baselines, "recompute", lambda *a, **k: 0)
    monkeypatch.setattr(
        cli.briefing_mod, "generate_and_save",
        lambda *, model: captured.setdefault("model", model) or Path("/tmp/b.md"),
    )
    result = runner.invoke(cli.main, ["brief", "--opus", "--no-notify"])
    assert result.exit_code == 0
    assert captured["model"] == cli.OPUS


def test_brief_default_fires_notification(runner, monkeypatch):
    # Wiring: without --no-notify the osascript subprocess is invoked.
    notify = {}
    monkeypatch.setattr(cli.daily_ingest, "pull", lambda *a, **k: _ok_pull_result())
    monkeypatch.setattr(cli.baselines, "recompute", lambda *a, **k: 0)
    monkeypatch.setattr(
        cli.briefing_mod, "generate_and_save", lambda *, model: Path("/tmp/b.md")
    )
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **k: notify.setdefault("cmd", cmd),
    )
    result = runner.invoke(cli.main, ["brief", "--no-pull"])
    assert result.exit_code == 0
    assert notify["cmd"][0] == "osascript"


# --------------------------------------------------------------------------
# group plumbing — verbose flag + help
# --------------------------------------------------------------------------

def test_verbose_flag_accepted(runner, dbp):
    # -v exercises the group callback's logging setup branch.
    result = runner.invoke(cli.main, ["-v", "status"])
    assert result.exit_code == 0


def test_mcp_stdio_inits_schema_and_runs(runner, dbp, monkeypatch):
    # Wiring only: init the schema, then hand off to mcp_server.run_stdio().
    from local_fitness.web import mcp_server

    ran = {}

    async def fake_run_stdio():
        ran["served"] = True

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    result = runner.invoke(cli.main, ["mcp-stdio"])
    assert result.exit_code == 0
    assert ran.get("served") is True


def test_help_lists_subcommands(runner):
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("pull", "backfill", "brief", "status", "config"):
        assert cmd in result.output
