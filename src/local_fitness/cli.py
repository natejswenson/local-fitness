"""CLI entry point: `fitness <subcommand>`.

Subcommands:
  setup                 — store Garmin creds in macOS Keychain, init DB
  pull                  — pull from Garmin Connect since last success
  backfill <zip>        — load historical Garmin data export
  recompute-baselines   — recompute rolling baselines + CTL/ATL/TSB
  recompute-body-battery — backfill body_battery_min/max from stored samples
  brief                 — pull + recompute + generate today's briefing
  brief-email           — pull + regenerate + email the brief (evening job)
  status                — show DB stats and last ingest run
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import date as Date
from pathlib import Path

import click
from dotenv import load_dotenv

# Load `.env` from the project root before anything else reads os.environ.
# Existing real env vars (set in the shell or by docker-compose) take
# precedence, so the container path is unaffected.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from . import db
from .agent import briefing as briefing_mod
from .ingest import auth, baselines
from .ingest import backfill as backfill_mod
from .ingest import daily as daily_ingest

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


@main.command("mcp-stdio")
def mcp_stdio():
    """Serve the fitness tools as an MCP server over stdio (local, auth-free).

    For Claude Desktop / `claude mcp add --transport stdio fitness -- \
    uv run fitness mcp-stdio`. The deployed HTTP endpoint lives at
    /mcp/ behind the bearer token (see web/server.py)."""
    import asyncio

    from . import db
    from .web import mcp_server

    # Parity with the HTTP path (which inits in the FastAPI lifespan): ensure the
    # schema exists so the live coach-persona resolution finds the settings table
    # on a fresh clone. (The persona wrap is fail-open regardless, but without
    # this stdio would degrade to no-persona until the DB is initialized.)
    db.init_schema()
    asyncio.run(mcp_server.run_stdio())


@main.command()
def setup():
    """One-time setup: init DB, store user name + Garmin credentials."""
    click.echo("Setting up local-fitness…\n")
    db.init_schema()
    click.echo(f"  ✓ DB ready at {db.get_db_path()}")

    current_name = db.get_setting("user_name")
    name = click.prompt(
        "Your name (used in briefs and chat)",
        default=current_name or "",
        show_default=bool(current_name),
    ).strip()
    if name:
        db.set_setting("user_name", name)
        click.echo(f"  ✓ Saved name: {name}")

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
        "  • `fitness brief`              – generate today's briefing"
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
        mfa_callback=lambda: click.prompt("Garmin MFA code", hide_input=False).strip(),
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


@main.command(name="recompute-body-battery")
def recompute_body_battery():
    """Backfill daily_metrics.body_battery_min/max from stored samples.

    One-time (or as-needed) repair for historical rows ingested before
    body_battery_min/max derivation existed — never runs automatically on
    `pull`. Idempotent: safe to re-run, never overwrites a non-NULL value.
    """
    n = daily_ingest.recompute_body_battery_minmax()
    click.echo(f"Backfilled body_battery_min/max for {n} date(s).")



def _notify(message: str) -> None:
    """Best-effort macOS notification (no-op failure on other platforms)."""
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{message}" with title "fitness"',
        ],
        check=False,
    )


@main.command()
@click.option("--no-pull", is_flag=True, help="Skip the pull step")
@click.option("--no-notify", is_flag=True, help="Skip the macOS notification")
@click.option("--opus", is_flag=True, help="Use Opus 4.7 instead of Sonnet 4.6")
@click.option(
    "--if-missing", is_flag=True,
    help="No-op if today's brief already exists (launchd backstop-fire mode)")
def brief(no_pull: bool, no_notify: bool, opus: bool, if_missing: bool):
    """Pull, recompute baselines, and generate today's briefing."""
    if if_missing:
        # Backstop mode: the launchd job fires at 06:30 AND a retry slot
        # later in the morning; the second fire (or a wake-coalesced pileup)
        # must not re-pull or burn a second LLM run when the first one
        # already saved. save_brief writes atomically, so an existing file
        # for today is a complete brief.
        from .agent.briefs import DEFAULT_BRIEFINGS_DIR

        today_path = DEFAULT_BRIEFINGS_DIR / f"{Date.today().isoformat()}.json"
        if today_path.exists():
            click.echo(f"Brief already exists for today ({today_path}) — skipping.")
            return
    if not no_pull:
        result = daily_ingest.pull()
        click.echo(f"Pull: {result['status']} ({result['days_pulled']} days)")
    baselines.recompute()
    try:
        path = briefing_mod.generate_and_save(model=OPUS if opus else SONNET)
    except Exception:
        # A failed generation used to be signaled only by the ABSENCE of the
        # success notification — invisible at 06:30. Fire a distinct failure
        # notification so a missed brief is a signal, not silence, then
        # re-raise so launchd still records a non-zero exit and the full
        # traceback still lands in brief.launchd.err.log.
        if not no_notify:
            _notify("Brief generation FAILED — check logs/brief.launchd.err.log")
        raise
    click.echo(f"Brief written to: {path}")
    if not no_notify:
        _notify("Today's brief is ready")


def _emailed_marker(target: Date) -> Path:
    """Marker proving today's brief email went out.

    The evening job fires twice (19:00 + a 20:00 backstop, mirroring the
    morning brief's 06:30/09:30 pattern), and unlike `brief --if-missing` the
    presence of a saved brief proves nothing here — the morning job already
    wrote one. Delivery needs its own record, or the backstop mails you a
    second copy every single night.
    """
    from .agent import briefs
    return briefs.DEFAULT_BRIEFINGS_DIR / f".emailed-{target.isoformat()}"


@main.command(name="brief-email")
@click.option("--date", "target_date", default=None,
              help="YYYY-MM-DD to send (default: today)")
@click.option("--no-pull", is_flag=True, help="Skip the Garmin pull step")
@click.option("--no-generate", is_flag=True,
              help="Email the saved brief as-is instead of regenerating it")
@click.option("--if-unsent", is_flag=True,
              help="No-op if this date's brief was already emailed (backstop mode)")
@click.option("--dry-run", "dry_run", type=click.Path(path_type=Path), default=None,
              help="Write the composed .eml to this path instead of sending")
@click.option("--to", default=None, help="Override the recipient(s), comma-separated")
@click.option("--no-notify", is_flag=True, help="Skip the macOS notification")
def brief_email(target_date: str | None, no_pull: bool, no_generate: bool,
                if_unsent: bool, dry_run: Path | None, to: str | None,
                no_notify: bool):
    """Pull, regenerate today's brief, and email it as a PRESS-styled report.

    The evening counterpart to `brief`. Regenerating is the default and it
    deliberately OVERWRITES `briefings/<today>.json`: the 19:00 brief sees a
    full day of Garmin data the 06:30 one could not, so it is the better
    record of the day. `save_brief` forces the date to today, so the overwrite
    is inherent rather than something this command arranges. The coach does
    not journal the day twice — `reflect` keys on `("brief", <date>)` and
    pre-checks `journal.has_event`.
    """
    import asyncio

    from .agent import branding, briefs, email_render, mailer
    from .agent import tools as agent_tools
    from .agent.schemas import Brief

    target = Date.fromisoformat(target_date) if target_date else Date.today()

    if if_unsent and _emailed_marker(target).exists():
        click.echo(f"Brief for {target} was already emailed — skipping.")
        return

    if not no_pull:
        result = daily_ingest.pull()
        click.echo(f"Pull: {result['status']} ({result['days_pulled']} days)")
        baselines.recompute()

    if not no_generate:
        try:
            briefing_mod.generate_and_save(model=SONNET)
        except Exception:
            # Same reasoning as `brief`: an unattended failure that only shows
            # up as a missing email is indistinguishable from "nothing to say".
            if not no_notify:
                _notify("Evening brief generation FAILED — check the logs")
            raise

    brief_path = briefs.DEFAULT_BRIEFINGS_DIR / f"{target.isoformat()}.json"
    if not brief_path.exists():
        click.echo(f"No saved brief for {target} — nothing to email.", err=True)
        sys.exit(1)
    brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))

    charts, plan_section = asyncio.run(
        agent_tools.assemble_brief_render_inputs(brief, target.isoformat()))

    theme = branding.load_theme()
    html_body = email_render.build_html(
        brief, {int(k) for k in charts}, plan_section, theme)
    text_body = email_render.build_text(brief, plan_section)
    subject = email_render.subject_for(brief)

    if dry_run is not None:
        cfg = mailer.placeholder_config()
    else:
        try:
            cfg = mailer.load_config(to)
        except mailer.MailNotConfigured as e:
            click.echo(f"  ⚠ {e}", err=True)
            sys.exit(2)

    msg = mailer.build_message(subject, html_body, text_body, charts, cfg)

    if dry_run is not None:
        dry_run.parent.mkdir(parents=True, exist_ok=True)
        dry_run.write_bytes(msg.as_bytes())
        click.echo(
            f"Dry run — wrote {dry_run} "
            f"({len(charts)} chart(s), {len(html_body)} chars of HTML). "
            "Nothing was sent."
        )
        return

    try:
        mailer.send(msg, cfg)
    except Exception:
        if not no_notify:
            _notify("Evening brief email FAILED — check the logs")
        raise
    # Written only after a confirmed send, so a failure leaves the backstop
    # slot free to try again rather than recording a delivery that never was.
    _emailed_marker(target).write_text(msg["Message-ID"] or "", encoding="utf-8")
    click.echo(f"Emailed {subject} to {', '.join(cfg.to)}")
    if not no_notify:
        _notify("Evening brief emailed")


@main.group()
def config():
    """View or set user settings (name, etc.)."""
    pass


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value, e.g. `fitness config set name Nate`."""
    db.init_schema()
    # Convenience aliases — `name` → `user_name`
    if key == "name":
        key = "user_name"
    db.set_setting(key, value)
    click.echo(f"  ✓ {key} = {value}")


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None):
    """Show one config value, or all of them if no key given."""
    db.init_schema()
    if key:
        if key == "name":
            key = "user_name"
        v = db.get_setting(key)
        click.echo(v if v is not None else "(unset)")
    else:
        settings = db.all_settings()
        if not settings:
            click.echo("(no settings configured)")
            return
        for k, v in settings.items():
            click.echo(f"  {k} = {v}")


@main.command()
@click.option("--port", default=8765, help="Port to bind (default 8765)")
@click.option(
    "--host",
    default="127.0.0.1",
    envvar="LOCAL_FITNESS_HOST",
    help="Host to bind (default 127.0.0.1, localhost-only). "
         "Set LOCAL_FITNESS_HOST=0.0.0.0 in the container to expose on the Docker network.",
)
@click.option("--reload", is_flag=True, help="Reload on code changes (dev mode)")
def serve(port: int, host: str, reload: bool):
    """Start the MCP server (streamable-HTTP transport at /mcp/)."""
    from .web.server import serve as serve_app
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
