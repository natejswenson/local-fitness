"""CLI entry point: `fitness <subcommand>`.

Subcommands:
  setup                 — store Garmin creds in macOS Keychain, init DB
  pull                  — pull from Garmin Connect since last success
  backfill <zip>        — load historical Garmin data export
  recompute-baselines   — recompute rolling baselines + CTL/ATL/TSB
  recompute-body-battery — backfill body_battery_min/max from stored samples
  brief                 — pull + recompute + generate today's briefing
  brief-email           — pull + regenerate + email the brief (evening job)
  plan-calendar         — put tomorrow's session on Google Calendar (evening job)
  calendar-auth         — one-time Google Calendar consent (prints a token)
  status                — show DB stats and last ingest run
"""
from __future__ import annotations

import logging
import os
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

    # Imported as app_config: this module defines a click GROUP named `config`
    # at module scope, which shadows the package's config module.
    from . import config as app_config
    from .agent import branding, briefs, email_render, mailer
    from .agent import tools as agent_tools
    from .agent.schemas import Brief

    target = Date.fromisoformat(target_date) if target_date else Date.today()

    if if_unsent and _emailed_marker(target).exists():
        click.echo(f"Brief for {target} was already emailed — skipping.")
        return

    # The conversational kill switch. Checked BEFORE the pull and the
    # regeneration, not just before the send: "stop emailing me the brief"
    # must not leave a job that still spends a Garmin pull and an LLM run
    # every night and then throws the result away. `--dry-run` ignores it, so
    # a disabled setup can still be inspected.
    if dry_run is None and not app_config.brief_email_enabled():
        click.echo(
            "Brief email is disabled (brief_email_enabled=false) — skipping. "
            "Re-enable with the update_brief_email_settings MCP tool."
        )
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


@main.command(name="plan-calendar")
@click.option("--from", "start_date", default=None,
              help="YYYY-MM-DD to sync from (default: today)")
@click.option("--dry-run", is_flag=True,
              help="Print the events instead of writing them to Google")
@click.option("--no-notify", is_flag=True, help="Skip the macOS notification")
def plan_calendar(start_date: str | None, dry_run: bool, no_notify: bool):
    """Make Google Calendar equal the remaining training plan (evening job).

    Writes every prescribed session from today through the last day of the
    active plan as an all-day event, updates the ones that changed, and
    DELETES the ones the plan no longer asks for. Nothing prescribed and
    nothing stale — that last half is what a push could never do.

    Runs on the same cadence as the brief email (launchd 19:05, backstop
    20:05) but as its OWN job, because it shares none of that job's cost: no
    Garmin pull, no LLM call, just a plan read and — in the steady state — a
    single HTTPS request. Its own failure domain means a Google outage can't
    make the email job look broken, and its own retry slot means a transient
    failure actually gets retried, which it would not as a tail step (by then
    the `.emailed-` marker already short-circuits the backstop).

    Since 0.53.0 the four plan-write MCP tools sync themselves, so this job is
    a RECONCILER rather than the only writer: it repairs a sync that failed
    mid-edit, a plan changed through `run_sql`, or a day that drifted. There is
    deliberately no dedupe flag — the reconcile is idempotent by construction,
    and a marker file would be a second source of truth that can drift from the
    first.
    """
    import json

    from .agent import calendar_sync, gcal

    try:
        result = calendar_sync.sync_active_plan(start=start_date, dry_run=dry_run)
    except gcal.CalendarNotConfigured as e:
        click.echo(f"  ⚠ {e}", err=True)
        sys.exit(2)
    except calendar_render_too_many() as e:
        click.echo(f"  ⚠ {e}", err=True)
        sys.exit(2)
    except Exception:
        if not no_notify:
            _notify("Plan calendar sync FAILED — check the logs")
        raise

    status = result["status"]
    if status == "dry_run":
        click.echo(json.dumps(result["events"], indent=2))
        click.echo(f"\nDry run — {len(result['events'])} event(s) from "
                   f"{result['start']} on plan #{result['plan_id']}. "
                   "Nothing was sent.")
        return
    if status in ("no_active_plan", "blocked"):
        # Both are ordinary outcomes, not failures: a fresh clone has no plan,
        # and the kill switch exists to be used.
        click.echo(f"Nothing to sync — {result['reason']}.")
        return

    click.echo(
        f"Calendar '{result['calendar_id']}' now matches plan "
        f"#{result['plan_id']} from {result['start']}: "
        f"{result['created']} created, {result['updated']} updated, "
        f"{result['deleted']} deleted, {result['unchanged']} unchanged."
    )
    if result["skipped_deleted_by_hand"]:
        # Otherwise a session that never appears has no explanation anywhere.
        click.echo(f"  {result['skipped_deleted_by_hand']} day(s) skipped — "
                   "you deleted those events; they are not being put back.")
    if result["changed_dates"]:
        click.echo(f"  changed: {', '.join(result['changed_dates'])}")
    written = result["created"] + result["updated"] + result["deleted"]
    if not no_notify and written:
        _notify(f"Training calendar updated ({written} day(s))")


def calendar_render_too_many():
    """`calendar_render.TooManyEvents`, imported lazily to keep the CLI's
    module-scope imports free of the agent package (which pulls the SDK)."""
    from .agent import calendar_render

    return calendar_render.TooManyEvents


#: Where Google sends the browser back after consent. A loopback redirect is
#: the documented flow for an "installed app" client; the port is chosen by the
#: OS at bind time and interpolated into the URI, so nothing has to be
#: pre-registered beyond `http://127.0.0.1` itself.
_OAUTH_LANDING = (
    "<html><body style='font-family:system-ui;padding:3rem'>"
    "<h2>local-fitness is connected.</h2>"
    "<p>You can close this tab and go back to the terminal.</p>"
    "</body></html>"
)


def _capture_oauth_redirect(build_url) -> tuple[str, dict[str, str]]:
    """Serve exactly one loopback request and return `(redirect_uri, query)`.

    The socket half of the consent flow, split out so the half that makes
    SECURITY decisions — the state check, the denied-consent branch — is
    testable without binding a port or driving a browser. This function is the
    only untested part of `calendar-auth`, and all it does is bind, open a
    browser and serve one request.

    ``build_url`` is a callback rather than a string because the redirect URI
    isn't known until the OS picks the port, and the URL has to carry it.
    """
    import http.server
    import urllib.parse
    import webbrowser

    caught: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's contract
            query = urllib.parse.urlparse(self.path).query
            caught.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_OAUTH_LANDING.encode())

        def log_message(self, *args):
            """Silence the default stderr access log — it would print the
            authorization code, which is a credential, into the terminal."""

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = build_url(redirect_uri)
        click.echo("Opening the Google consent screen…")
        click.echo(f"If it doesn't open, paste this into a browser:\n\n{url}\n")
        webbrowser.open(url)
        server.handle_request()
    return redirect_uri, caught


@main.command(name="calendar-auth")
def calendar_auth():
    """One-time Google Calendar consent — prints a refresh token for `.env`.

    Runs the installed-app loopback flow with PKCE: opens the consent screen,
    catches the redirect on a throwaway localhost server, and exchanges the
    code. The token is PRINTED rather than written, because `.env` is the one
    file in this repo that must never be edited by a program — it holds every
    other credential, and a bug here would be a bug that eats them.

    Prerequisites (docs/google-calendar.md): a Google Cloud project with the
    Calendar API enabled and an OAuth client of type "Desktop app", with the
    consent screen set to "In production" — a Testing-mode app has its refresh
    tokens expired by Google every 7 days.
    """
    import base64
    import hashlib
    import secrets

    from .agent import gcal

    client_id = os.environ.get("LOCAL_FITNESS_GCAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        click.echo(
            "  ⚠ Set LOCAL_FITNESS_GCAL_CLIENT_ID and "
            "LOCAL_FITNESS_GCAL_CLIENT_SECRET in <repo>/.env first — they come "
            "from the OAuth client you create in Google Cloud. Steps: "
            "docs/google-calendar.md",
            err=True,
        )
        sys.exit(2)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    redirect_uri, caught = _capture_oauth_redirect(
        lambda uri: gcal.authorization_url(client_id, uri, state, challenge))

    if caught.get("state") != state:
        # A mismatched state means the response didn't come from the request we
        # made — the whole point of the nonce.
        click.echo("  ⚠ State mismatch — aborting without exchanging the code.",
                   err=True)
        sys.exit(1)
    if "code" not in caught:
        click.echo(f"  ⚠ No authorization code returned "
                   f"({caught.get('error', 'consent was denied or cancelled')}).",
                   err=True)
        sys.exit(1)

    token = gcal.exchange_code(
        client_id, client_secret, caught["code"], redirect_uri, verifier)
    click.echo("\n  ✓ Connected. Add this line to <repo>/.env:\n")
    click.echo(f"LOCAL_FITNESS_GCAL_REFRESH_TOKEN={token}\n")
    click.echo("Then check it end to end with:  uv run fitness plan-calendar")


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
