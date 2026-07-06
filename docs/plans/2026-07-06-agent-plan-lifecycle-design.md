---
title: "Agent-owned training plan lifecycle — commit + discard drafts"
date: "2026-07-06"
status: draft (pending quality-gate)
---

# Agent-owned training plan lifecycle: commit + discard

## Goal

Give the agent two new MCP tools — `commit_training_plan` and
`discard_training_plan_draft` — so activating or rejecting a draft training
plan no longer requires a click in the web UI. This extends the
already-adopted "agent owns plan writes" model (per-day prescription edits
via `update_plan_workout`) to the plan *lifecycle* itself: propose → revise →
commit/discard, entirely from the agent.

## Background / why now

Today, `propose_training_plan`/`revise_training_plan` can create and edit a
DRAFT, and `update_plan_workout` can re-prescribe a single day on the ACTIVE
plan — but nothing in the MCP tool surface can flip a draft to active (or
discard it). That step has always required the web UI's commit/delete
buttons, a deliberate human-review gate documented in CLAUDE.md ("the user
commits via the UI").

The user (sole operator of this personal, single-user app) now wants to do
everything through the agent, including this step. Investigation found the
underlying capability already exists and is already correct:
`plans.commit_plan(plan_id, now)` flips draft→active, archives the prior
active plan, and is race-safe via the `idx_one_active_plan` partial unique
index. It's already used by `POST /api/plan/{plan_id}/commit` for the UI's
commit button. So this is not a new persistence/schema design — it's
exposing an existing, already-tested capability through a new tool, plus one
new sibling function for the symmetric "discard a draft" case that has no
equivalent today.

## Architecture

No new tables, columns, or state-machine transitions. Same
`training_plans.status` values (`draft` / `active` / `archived`), same
`idx_one_active_plan` race backstop.

### `plans.py` (persistence layer)

`commit_plan` needs zero changes — reused as-is.

One new sibling function:

```python
def discard_draft(plan_id: int, db_path: Path | None = None) -> None:
    """Archive a DRAFT plan without activating it. Guards status=='draft'
    like commit_plan/revise_draft — refuses active/archived targets so an
    agent call can never archive the live plan by mistake."""
```

Raises the existing `PlanNotFoundError` / `NotDraftError` exceptions — no new
exception types.

This is a **new function**, not a change to the existing generic
`delete_plan` (which the UI still uses to delete either a draft *or* the
active plan — that broader, unguarded behavior stays UI-only; see "Out of
scope").

### `agent/tools.py` (MCP layer)

Two new `@tool`-decorated functions, same shape as `revise_training_plan`:

```python
@tool(
    "commit_training_plan",
    "Activate a DRAFT training plan, archiving whichever plan is currently "
    "active. Only call this when the user has explicitly asked you to "
    "activate a specific draft in this conversation — never proactively. "
    "The web UI's commit button still works too; this is the agent-side "
    "equivalent.",
    {"type": "object", "properties": {"plan_id": {"type": "integer"}}, "required": ["plan_id"]},
)
async def commit_training_plan(args: dict) -> dict:
    ...  # validate plan_id is int; plans.commit_plan(plan_id, now=...);
         # catch PlanNotFoundError/NotDraftError -> _err(str(e));
         # else _text({"plan_id": plan_id, "status": "active"})
```

```python
@tool(
    "discard_training_plan_draft",
    "Discard (archive) the DRAFT training plan without activating it. Only "
    "works on a draft — refuses to touch the active plan (call "
    "commit_training_plan on a new draft to replace it instead). Only call "
    "when the user explicitly asks to drop or reject a draft.",
    {"type": "object", "properties": {"plan_id": {"type": "integer"}}, "required": ["plan_id"]},
)
async def discard_training_plan_draft(args: dict) -> dict:
    ...  # validate plan_id is int; plans.discard_draft(plan_id);
         # catch PlanNotFoundError/NotDraftError -> _err(str(e));
         # else _text({"plan_id": plan_id, "status": "archived"})
```

Both added to `ALL_TOOLS`. Neither added to `_READ_ONLY_TOOL_NAMES` — that's
an explicit allowlist (not a denylist), so exclusion is automatic simply by
not listing them there. Matches `update_plan_workout`'s existing precedent.

## API Surface

| Symbol | Layer | Signature |
|---|---|---|
| `plans.discard_draft` | persistence | `(plan_id: int, db_path: Path \| None = None) -> None` |
| `commit_training_plan` | MCP tool | `{plan_id: int} -> {plan_id, status: "active"}` |
| `discard_training_plan_draft` | MCP tool | `{plan_id: int} -> {plan_id, status: "archived"}` |

Reused, unchanged: `plans.commit_plan`, `PlanNotFoundError`, `NotDraftError`,
`_err`/`_text` tool-response helpers.

## Invariants

**Checkable by inspection:**
- `discard_draft` never mutates a row whose status isn't `draft` (raises
  `NotDraftError` otherwise) — mirrors `commit_plan`'s own guard.
- Neither new tool is added to `_READ_ONLY_TOOL_NAMES`.
- Neither tool accepts `status` as caller input — the action taken is implied
  by which tool is called, matching `revise_draft`'s "status is deliberately
  excluded from editable fields" pattern.

**Testable:**
- `commit_training_plan` activates a real draft and archives the prior
  active plan; errors (no mutation) on missing / already-active /
  already-archived `plan_id`.
- `discard_training_plan_draft` archives a draft; errors (no mutation) when
  targeting the active plan or an already-archived plan — this is the key
  new safety property versus the UI's `delete_plan`, which has no such
  guard.
- `test_tools_registered()` extended with both new names.
- New parity tests mirroring
  `test_update_plan_workout_is_a_write_tool_not_in_brief` for both tools
  (registered in `ALL_TOOLS` / `allowed_tool_names()`, absent from
  `read_only_tool_names()`).

## Failure modes / edge cases

- `plan_id` refers to nothing → `PlanNotFoundError` → `_err`, no mutation.
- `plan_id` refers to the active plan, passed to the discard tool →
  `NotDraftError` → `_err` (protects against accidentally wiping the live
  plan with no replacement queued).
- `plan_id` refers to an already-archived plan (stale reference from an
  earlier turn, e.g. double-commit/double-discard) → `NotDraftError` →
  `_err`, idempotent-safe.
- Concurrent commit race → already handled by `idx_one_active_plan`; this is
  pre-existing `commit_plan` behavior (shared with the REST endpoint),
  unchanged by this design. Not addressed further — negligible risk in a
  single-user, single-session context.

## Testing strategy

- `tests/test_plans_db.py`: add `discard_draft` cases — archives a draft;
  raises `NotDraftError` on an active target; raises `NotDraftError` on an
  already-archived target; raises `PlanNotFoundError` on a missing
  `plan_id`. Same fixture style as the existing `test_commit_*`/`test_delete_*`
  cases in that file.
- `tests/test_plan_tools.py`: add tool-layer cases for both new tools
  (success path + each error path), extend `test_tools_registered()`, add
  the two read-only-allowlist parity tests.
- No changes to `tests/test_web_plan.py` (REST layer untouched) or
  `tests/test_security.py` (no new HTTP surface; these are MCP tools reached
  only through the already-authenticated agent session — same trust
  boundary as the existing plan-write tools).

## Documentation updates (same PR)

- **CLAUDE.md**, "Answering fitness questions" section: replace "Structure
  changes (whole new plan) still go through propose_training_plan/
  revise_training_plan (drafts)" with wording reflecting that
  `commit_training_plan`/`discard_training_plan_draft` now let the agent
  finish the lifecycle directly — the UI's commit/delete buttons become an
  optional parity path, not a required step.
- **`plans.py`** module-level comment block (currently states activation is
  UI-only): update to describe the two-tier reality — `commit_plan`/
  `discard_draft` are agent-writable via dedicated tools; the broader,
  unguarded `delete_plan` (draft-or-active) remains REST/UI-only.

## Out of scope (explicit, YAGNI)

- No "abandon the active plan with no replacement" agent tool. That stays
  UI-only via the existing unguarded `delete_plan`. Wiping the only active
  plan with nothing queued to replace it is a meaningfully bigger blast
  radius than draft↔draft or draft→active, and it wasn't requested.
- No `confirm: true` parameter on either tool. The guardrail is the tool
  description's usage guidance (prompt-level), the same mechanism
  `propose_training_plan`'s "ground it first" instruction already relies on.
