"""CLI entry point: `fitness <subcommand>`.

Subcommands:
  setup                 — store Garmin creds in macOS Keychain, init DB
  pull                  — pull from Garmin Connect since last success
  backfill <zip>        — load historical Garmin data export
  recompute-baselines   — recompute rolling baselines + CTL/ATL/TSB
  brief                 — pull + recompute + generate today's briefing
  chat                  — interactive REPL with the agent
  ask "<question>"      — one-shot question to the agent
  status                — show DB stats and last ingest run
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import date as Date
from pathlib import Path

import click

from . import db
from .agent import briefing as briefing_mod
from .agent import chat as chat_mod
from .ingest import auth, backfill as backfill_mod, baselines, daily as daily_ingest

SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
def main(verbose: bool):
    _setup_logging(verbose)


@main.command()
def setup():
    """One-time setup: init DB and store Garmin credentials in macOS Keychain."""
    click.echo("Setting up local-fitness…\n")
    db.init_schema()
    click.echo(f"  ✓ DB ready at {db.get_db_path()}")

    existing = auth.get_credentials()
    if existing:
        if not click.confirm(f"Garmin creds already stored for {existing[0]}. Replace?"):
            click.echo("Keeping existing credentials.")
            return
    auth.prompt_and_store()
    click.echo("  ✓ Garmin creds stored in macOS Keychain (service: local-fitness-garmin)")
    click.echo(
        "\nNext:\n"
        "  • `fitness pull`               – fetch live data\n"
        "  • `fitness backfill <zip>`     – load historical export ZIP\n"
        "  • `fitness brief`              – generate today's briefing\n"
        "  • `fitness chat`               – interactive REPL"
    )


@main.command()
@click.option("--from", "force_from", default=None,
              help="YYYY-MM-DD: ignore last-success and pull from this date")
@click.option("--through", default=None,
              help="YYYY-MM-DD: pull through this date (default today)")
def pull(force_from: str | None, through: str | None):
    """Pull from Garmin Connect; catches up since last successful run."""
    result = daily_ingest.pull(
        through=Date.fromisoformat(through) if through else None,
        force_from=Date.fromisoformat(force_from) if force_from else None,
    )
    click.echo(
        f"Status: {result['status']} · days: {result['days_pulled']} · "
        f"activities: {result.get('activities_loaded', 0)} · last: {result['last_date']}"
    )
    if result.get("error"):
        click.echo(f"  ⚠ error: {result['error']}", err=True)
        sys.exit(1)


@main.command()
@click.argument("zip_path", type=click.Path(exists=True, path_type=Path))
def backfill(zip_path: Path):
    """Load historical data from a Garmin Connect 'Request your data' ZIP."""
    counts = backfill_mod.backfill(zip_path)
    click.echo("Backfill complete:")
    for k, v in counts.items():
        click.echo(f"  {k}: {v}")


@main.command(name="recompute-baselines")
@click.option("--lookback", default=90, help="Days of history to recompute")
def recompute_baselines(lookback: int):
    """Recompute 60-day rolling baselines and CTL/ATL/TSB."""
    n = baselines.recompute(lookback_days=lookback)
    click.echo(f"Recomputed baselines for {n} dates.")


@main.command()
@click.option("--no-pull", is_flag=True, help="Skip the pull step")
@click.option("--no-notify", is_flag=True, help="Skip the macOS notification")
@click.option("--opus", is_flag=True, help="Use Opus 4.7 instead of Sonnet 4.6")
def brief(no_pull: bool, no_notify: bool, opus: bool):
    """Pull, recompute baselines, and generate today's briefing."""
    if not no_pull:
        result = daily_ingest.pull()
        click.echo(f"Pull: {result['status']} ({result['days_pulled']} days)")
    baselines.recompute()
    path = briefing_mod.generate_and_save(model=OPUS if opus else SONNET)
    click.echo(f"Brief written to: {path}")
    if not no_notify:
        subprocess.run(
            [
                "osascript",
                "-e",
                'display notification "Today\'s brief is ready" with title "fitness"',
            ],
            check=False,
        )


@main.command(name="chat")
@click.option("--opus", is_flag=True, help="Use Opus 4.7 instead of Sonnet 4.6")
def chat_cmd(opus: bool):
    """Interactive REPL with the fitness agent."""
    chat_mod.run(model=OPUS if opus else SONNET)


@main.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--opus", is_flag=True, help="Use Opus 4.7 instead of Sonnet 4.6")
def ask(question: tuple[str, ...], opus: bool):
    """Ask the agent a one-shot question."""
    chat_mod.ask(" ".join(question), model=OPUS if opus else SONNET)


@main.command()
@click.option("--port", default=8765, help="Port to bind (default 8765)")
@click.option("--host", default="127.0.0.1", help="Host to bind (default 127.0.0.1, localhost-only)")
@click.option("--reload", is_flag=True, help="Reload on code changes (dev mode)")
@click.option("--open", "open_browser", is_flag=True, help="Open browser on start")
def serve(port: int, host: str, reload: bool, open_browser: bool):
    """Start the web UI + API server."""
    from .web.server import serve as serve_app
    if open_browser:
        import threading
        import time
        import webbrowser
        def _open():
            time.sleep(1)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=_open, daemon=True).start()
    serve_app(host=host, port=port, reload=reload)


@main.command()
def status():
    """Show DB stats and last ingest run."""
    db.init_schema()
    with db.connect() as conn:
        rows = {}
        for table in ("daily_metrics", "activities", "baselines",
                      "body_battery_samples", "stress_samples"):
            r = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            rows[table] = r["n"]
        last_run = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    click.echo(f"DB: {db.get_db_path()}\n")
    click.echo("Row counts:")
    for k, v in rows.items():
        click.echo(f"  {k:24s} {v:>10,}")
    click.echo()
    if last_run:
        click.echo(f"Last ingest run: {dict(last_run)}")
    else:
        click.echo("No ingest runs yet.")


if __name__ == "__main__":
    main()
