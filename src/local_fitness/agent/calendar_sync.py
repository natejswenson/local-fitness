"""Keep Google Calendar equal to the remaining training plan.

The orchestration layer: ``calendar_render`` decides *what should be there*
(pure), ``gcal`` moves bytes (transport), and this module is the thin thing in
between that reads the DB, calls both, and reports what happened.

**There is exactly ONE function that writes the calendar** — ``sync_active_plan``
— and every trigger goes through it: the four plan-write MCP tools, and the
19:05 launchd job. That is a deliberate constraint rather than an accident of
factoring. A "just update the day that changed" fast path would be a second
definition of what the calendar should contain, and the two would drift the
first time a plan edit had a side effect on another day. One full reconcile
costs one extra list request in the steady state, and buys the guarantee that
every write is the same code.

The sync is a PROJECTION of the plan, never a source of truth. The plan is
already committed by the time this runs, so every failure here is reported and
swallowed — see ``sync_after_plan_write``.
"""
from __future__ import annotations

import logging
from datetime import date as Date
from pathlib import Path

from .. import config, db, plans
from . import calendar_render, gcal

LOG = logging.getLogger(__name__)


def blocked_reason() -> str | None:
    """Why a sync would not run, or ``None`` if it would.

    Checked BEFORE the plan read and before anything opens a socket, so a
    disabled or unconfigured deployment costs nothing at all — which is what
    lets the MCP hooks call this on every plan edit without thinking about it.

    Credentials are checked FIRST because that check is a pure env read while
    the settings check opens the DB. A clone that never set this up is the
    common case, and it should not pay a connection on every plan edit forever
    to be told something it could learn from ``os.environ``.
    """
    if not gcal.credentials_configured():
        return "Google Calendar credentials are not configured in <repo>/.env"
    if not config.plan_calendar_enabled():
        return "disabled via settings (plan_calendar_enabled=false)"
    return None


def _summarize(actions: dict, plan_id: int) -> dict:
    """Counts plus the dates that actually moved.

    Counts alone can't answer "did MY edit land", which is the question the
    caller of a plan-write tool has; the changed dates can. Unchanged days are
    counted and not listed — on a 42-day plan that list is the whole plan and
    tells you nothing.
    """
    def dates(events):
        return sorted(e["start"]["date"] for e in events)

    return {
        "plan_id": plan_id,
        "created": len(actions["create"]),
        "updated": len(actions["update"]),
        "deleted": len(actions["delete"]),
        "unchanged": len(actions["unchanged"]),
        # A day the plan still wants but that was deleted on the calendar by
        # hand. Reported rather than silently skipped: it is the only way to
        # explain a session that never appears.
        "skipped_deleted_by_hand": len(actions["skipped_cancelled"]),
        "changed_dates": sorted(
            set(dates(actions["create"]) + dates(actions["update"])
                + [calendar_render._event_date(e) for e in actions["delete"]])
        ),
    }


def sync_active_plan(
    start: str | None = None,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> dict:
    """Make the calendar equal the active plan from ``start`` (default: today).

    Returns a summary dict; ``status`` is ``synced``, ``no_active_plan``,
    ``dry_run`` or ``blocked``. Raises only on a genuine transport failure —
    every *expected* nothing-to-do outcome is a status, not an exception,
    because those happen constantly (no plan yet, a fresh clone, the switch
    turned off) and a caller should not need a try/except to survive normal.

    Steady state is ONE request: the list comes back, everything matches, and
    nothing is written.
    """
    start = start or Date.today().isoformat()

    if not dry_run and (reason := blocked_reason()):
        return {"status": "blocked", "reason": reason}

    with db.connect(db_path) as conn:
        active = plans.get_active_plan(conn=conn)
    if active is None:
        return {"status": "no_active_plan", "reason": "no active training plan"}

    plan_id = active["plan_id"]
    desired = calendar_render.build_plan_events(active["workouts"], plan_id, start)

    if dry_run:
        return {"status": "dry_run", "plan_id": plan_id, "events": desired,
                "start": start}

    cfg = gcal.load_config()
    token = gcal.access_token(cfg)
    existing = gcal.list_plan_events(cfg, token, plan_id)
    actions = calendar_render.reconcile(desired, existing, start)

    # Creates and updates both go through `upsert_event`: the reconcile already
    # decided WHETHER to write, and upsert owns HOW — including the 409 race
    # against a listing that went stale between the two requests.
    for event in actions["create"] + actions["update"]:
        gcal.upsert_event(event, cfg, token)
    for row in actions["delete"]:
        gcal.delete_event(row["id"], cfg, token)

    summary = _summarize(actions, plan_id)
    summary.update(status="synced", calendar_id=cfg.calendar_id, start=start)
    LOG.info("plan_calendar sync plan=%s created=%s updated=%s deleted=%s "
             "unchanged=%s", plan_id, summary["created"], summary["updated"],
             summary["deleted"], summary["unchanged"])
    return summary


def remove_plan_events(
    plan_id: int, on_or_after: str | None = None
) -> dict:
    """Delete a plan's FUTURE events — the abandon path.

    Abandoning a plan has to clear the calendar, or it keeps confidently
    prescribing work from a plan that no longer governs anything, and the only
    hint is a description line naming a plan number nobody remembers.

    Bounded on both sides: ``on_or_after`` (default today) protects history the
    same way ``reconcile`` does, and ``gcal.list_plan_events`` scopes to this
    plan's own tagged events so nothing else on the calendar is reachable from
    here. A tombstone is left alone rather than re-deleted.
    """
    on_or_after = on_or_after or Date.today().isoformat()
    if reason := blocked_reason():
        return {"status": "blocked", "reason": reason}

    cfg = gcal.load_config()
    token = gcal.access_token(cfg)
    doomed = [
        e for e in gcal.list_plan_events(cfg, token, plan_id)
        if calendar_render._event_date(e) >= on_or_after
        and e.get("status") != calendar_render.CANCELLED
    ]
    for row in doomed:
        gcal.delete_event(row["id"], cfg, token)

    LOG.info("plan_calendar removed plan=%s deleted=%s", plan_id, len(doomed))
    return {"status": "removed", "plan_id": plan_id, "deleted": len(doomed),
            "from": on_or_after}


def sync_after_plan_write() -> dict | None:
    """The MCP hook. Never raises, never blocks the write it follows.

    Returns ``None`` when no sync was attempted — no credentials, or the kill
    switch is off. ``None`` means "omit the key": a clone that never set this
    up should not get a line of calendar noise appended to every plan edit it
    ever makes.

    Everything else is caught and reported. The plan write already committed
    by the time this runs and is the source of truth; the calendar is a
    projection of it. Letting a Google outage raise here would turn a
    successful plan edit into a failed tool call, and the model would very
    reasonably retry the edit — which is how a transport problem becomes a
    data problem.
    """
    if blocked_reason():
        return None
    try:
        return sync_active_plan()
    except Exception as e:  # noqa: BLE001 — a projection must never fail its source
        LOG.warning("plan_calendar sync failed after a plan write: %s", e)
        return {"status": "error", "error": str(e)}


def remove_after_abandon(plan_id: int) -> dict | None:
    """``remove_plan_events`` with ``sync_after_plan_write``'s contract."""
    if blocked_reason():
        return None
    try:
        return remove_plan_events(plan_id)
    except Exception as e:  # noqa: BLE001 — same reasoning as above
        LOG.warning("plan_calendar cleanup failed after abandon: %s", e)
        return {"status": "error", "error": str(e)}


def sync_after_commit(superseded_plan_id: int | None) -> dict | None:
    """Committing a draft ARCHIVES the prior active plan — clear its events too.

    Without this half, activating a new plan leaves the old plan's remaining
    sessions on the calendar forever. They would sit beside the new ones, both
    tagged and both looking authoritative, and the only thing distinguishing
    them is a plan number in the description. Two plans on one calendar is
    worse than no plan on it.

    The events cannot simply be overwritten: ``event_id`` keys on ``plan_id``,
    so the new plan's events land on entirely different ids by design (that is
    what stops a sync from rewriting a previous plan's history). Superseded
    events therefore have to be deleted explicitly, which is exactly what
    ``remove_plan_events`` already does for the abandon path.

    Same fail-soft contract as ``sync_after_plan_write``: the commit is done.
    """
    if blocked_reason():
        return None
    try:
        removed = (remove_plan_events(superseded_plan_id)
                   if superseded_plan_id is not None else None)
        result = sync_active_plan()
        if removed and removed.get("deleted"):
            result["superseded_plan_deleted"] = removed["deleted"]
        return result
    except Exception as e:  # noqa: BLE001 — same reasoning as above
        LOG.warning("plan_calendar sync failed after a commit: %s", e)
        return {"status": "error", "error": str(e)}
