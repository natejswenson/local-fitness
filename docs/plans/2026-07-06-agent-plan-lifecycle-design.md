---
title: "Agent-owned training plan lifecycle — commit + discard drafts"
date: "2026-07-06"
status: quality-gated PASS (9 rounds incl. look-harder; weighted score 2→4→4→3→2→3→1→0→0; 0 Fatal / 0 Significant)
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
buttons — a deliberate human-review gate. `revise_training_plan`'s own tool
description (`src/local_fitness/agent/tools.py:1251`) states this
explicitly: "Cannot change a plan's status — the user commits via the UI."
CLAUDE.md documents the same concept at a higher level ("Structure changes
(whole new plan) still go through propose_training_plan/revise_training_plan
(drafts)") but that literal sentence is the tool description's, not
CLAUDE.md's.

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
    """Archive a DRAFT plan without activating it.

    Unlike commit_plan's check-then-write (SELECT status, then an
    unconditional UPDATE), discard_draft folds the status guard into the
    UPDATE's WHERE clause itself, closing the TOCTOU at the SQL layer
    instead of via a separate SELECT:

        UPDATE training_plans SET status='archived'
        WHERE plan_id=? AND status='draft'

    If cursor.rowcount == 0, the row either doesn't exist or isn't
    currently a draft; a follow-up existence check disambiguates which
    of PlanNotFoundError / NotDraftError to raise. Refuses active/
    archived targets so an agent call can never archive the live plan by
    mistake, and — because the guard is atomic with the write — closes
    the same-plan_id race against a concurrent commit_training_plan (see
    "Failure modes / edge cases")."""
```

**Implementation invariant:** the follow-up existence check that
disambiguates `PlanNotFoundError` from `NotDraftError` must run on the same
connection/transaction as the failed `UPDATE`, not a fresh `db.connect()`.
This is *not* about closing a data-integrity race — that race is already
fully closed the moment the conditional `UPDATE ... WHERE plan_id=? AND
status='draft'` returns `rowcount == 0`; no further write happens in
`discard_draft`, only a read to pick which exception to raise, so a fresh
connection could at worst mislabel the exception (e.g. `PlanNotFoundError`
vs. `NotDraftError`), not clobber data. There's no race-safety reason to open
a second connection either: staying on the one already open is just ordinary
code hygiene, consistent with every other DB-touching function in
`plans.py`'s persistence section (`insert_draft`, `revise_draft`,
`update_active_workout`, `commit_plan`, `delete_plan`, `get_plan`,
`_get_by_status`, `load_activities_by_date`, `best_recent_effort`), each of
which does its work inside a single `with db.connect(db_path) as conn:`
block per call — a follow-up read has no reason to break that pattern by
opening a second connection. (The file's first half (lines 1-391 of 806, roughly
48%) — per its own docstring, "Training-plan pure logic — no I/O" — covers functions like
`validate_plan_input`, `classify_workout`, `grade_workout`, `riegel_predict`,
`weekly_mileage`, and `score_plan`, which never open a connection at all, so
the one-connection-per-call pattern only applies to the persistence
section.)

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
  by which tool is called, matching `revise_training_plan`'s (the MCP tool in
  `agent/tools.py`, not `plans.py`'s `revise_draft`) existing inline comment:
  "status is deliberately NOT among the readable fields — it can never be set
  here."
- `discard_draft`'s status guard is applied atomically: the `WHERE
  status='draft'` predicate lives inline on the `UPDATE` itself, not as a
  separate SELECT followed by an unconditional write — so there is no
  window between check and write for another writer to invalidate the
  precondition.

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
  `_err` — fails safely without mutating state (error-safe, not idempotent:
  a repeat call raises rather than silently no-opping).
- Concurrent commit race (cross-draft) → already handled by
  `idx_one_active_plan`; this is pre-existing `commit_plan` behavior (shared
  with the REST endpoint), unchanged by this design. The losing
  `commit_plan` call raises a raw `sqlite3.IntegrityError` on the unique-index
  violation, which neither the REST handler's nor the new MCP tool's
  exception catches handle — it would propagate uncaught, the same
  uncaught-exception characteristic as the lock-contention bullet below.
  Not addressed further — negligible risk given that the realistic trigger
  is a cross-surface race (e.g. the web UI open in one browser tab while an
  agent chat session is also active, or two tool calls landing in the same
  LLM turn), not literally two simultaneous sessions.
- Concurrent commit/discard race (same `plan_id`, cross-tool) —
  **discard-side only is closed.** `commit_training_plan(N)` and
  `discard_training_plan_draft(N)` firing near-simultaneously could
  otherwise both observe `status='draft'` before either writes. Folding the
  `status='draft'` guard into `discard_draft`'s `UPDATE ... WHERE` clause
  itself (see "`plans.py`" above) closes the case where `discard_draft`
  would clobber a plan `commit_plan` just activated — the guard's rowcount
  comes back 0 and `discard_draft` raises `NotDraftError` instead of
  archiving a live plan. This is distinct from the `idx_one_active_plan`
  race above (which guards against two *different* drafts both becoming
  active). The **reverse interleaving is not closed**: `commit_plan` itself
  (unchanged, out of scope for this design) does its own SELECT-then-write
  with no `WHERE status='draft'` guard on its final activating `UPDATE`, so
  if a `discard_training_plan_draft` call archives the row in the window
  between `commit_plan`'s SELECT and its final `UPDATE`, `commit_plan` will
  either blindly reactivate a plan the user just had discarded, or hit the
  lock-contention path below and raise an uncaught
  `sqlite3.OperationalError`. This gap is pre-existing, accepted behavior of
  `commit_plan` — not a new regression introduced here — and is called out
  again in "Testing strategy" as not practically test-coverable given this
  codebase's conventions.
- Lock-contention timeout → SQLite's single-writer lock is database/file-level,
  not per-row (WAL mode doesn't change this for writers) — so a concurrent
  `commit_training_plan(N)` and `discard_training_plan_draft(N)` (the
  same-`plan_id` scenario above) serialize against each other the same as any
  other two writers would, bounded by `sqlite3.connect()`'s default 5s busy
  timeout. If contention outlasts that window, the losing
  call raises a raw `sqlite3.OperationalError: database is locked`, which
  neither `discard_draft` nor the MCP tools' `except (PlanNotFoundError,
  NotDraftError)` catches — it would propagate uncaught. Not addressed
  further — negligible risk given the same realistic-trigger framing as the
  cross-draft race above (a cross-surface race, not literally two
  simultaneous sessions).

## Testing strategy

- `tests/test_plans_db.py`: add `discard_draft` cases — archives a draft;
  raises `NotDraftError` on an active target; raises `NotDraftError` on an
  already-archived target; raises `PlanNotFoundError` on a missing
  `plan_id`. Same fixture style as the existing `test_commit_*`/`test_delete_*`
  cases in that file. (The atomicity property of the guard — a single SQL
  statement with an inline predicate, not a separate SELECT-then-write — is
  a structural claim verifiable by inspection, not something a dedicated
  test can exercise beyond the "raises `NotDraftError` on an active target"
  case above; see "Invariants > Checkable by inspection.")
- A true two-writer concurrency test (two threads/connections actually
  racing against the same row) is not practical to add here: SQLite
  serializes writers at the database-file level, this codebase has no
  thread-based race tests anywhere in its existing suite, and simulating
  the interleaving from a single test thread would only re-assert the
  already-covered rowcount/error-path behavior above rather than genuinely
  exercise concurrent execution. The `commit_plan` reverse-interleaving gap
  described in "Failure modes / edge cases" is therefore accepted as a
  documented, untested residual risk — not silently tested away.
- `tests/test_plan_tools.py`: add tool-layer cases for both new tools
  (success path + each error path), extend `test_tools_registered()`, add
  the two read-only-allowlist parity tests. Also add a wrong-type case for
  each tool — a non-integer `plan_id` (e.g. a string) must be rejected by
  the `isinstance` validation before it ever reaches `plans.commit_plan`/
  `plans.discard_draft`, mirroring the existing `isinstance` check pattern
  in `revise_training_plan`.
- No changes to `tests/test_web_plan.py` (REST layer untouched) or
  `tests/test_security.py` (no new HTTP surface; these are MCP tools reached
  only through the already-authenticated agent session — same trust
  boundary as the existing plan-write tools). To be explicit about that
  boundary: `web/mcp_server.py`'s `build_server()` exposes the full
  `ALL_TOOLS` list — including these two new tools — over both stdio and
  the auth-gated `/mcp/` HTTP endpoint. That is pre-existing, identical
  exposure to `propose_training_plan`/`revise_training_plan`/
  `update_plan_workout` today; this design doesn't change that surface or
  introduce a new gap, it just adds two more names to an already-exposed
  list.

## Documentation updates (same PR)

- **CLAUDE.md**, "Answering fitness questions" section: replace "Structure
  changes (whole new plan) still go through propose_training_plan/
  revise_training_plan (drafts)" with wording reflecting that
  `commit_training_plan`/`discard_training_plan_draft` now let the agent
  finish the draft→active/discard lifecycle directly. Precisely: only
  **commit** becomes agent-owned with the UI's commit button as an optional
  parity path, not a required step — activating a draft always has a
  same-effect agent alternative now. **Delete/abandon stays UI-only and
  mandatory**: `discard_training_plan_draft` only ever targets drafts, so
  archiving the ACTIVE plan with nothing queued to replace it still has no
  agent tool and still requires the UI's delete button (per "Out of
  scope").
- **`plans.py`** module-level comment block (~lines 391-404): currently
  states that "activation/deletion (commit_plan/delete_plan) flip status"
  and, separately, that per-day prescriptions via `update_active_workout`
  are agent-writable ("the agent is the plan write path and the web UI is
  view-only") — it doesn't currently make any claim about who can flip
  status, it simply doesn't mention agent-writability there. Update it to
  add that same agent-writable note alongside the status-flip sentence:
  `commit_plan`/`discard_draft` are now agent-writable via dedicated tools,
  while the broader, unguarded `delete_plan` (draft-or-active) remains
  REST/UI-only.
- **`src/local_fitness/web/server.py`** (~lines 376-379): the comment there
  reads approximately "commit/delete are the human-driven activation/
  soft-delete actions — the agent has no tool for either." That becomes
  stale for "commit" once `commit_training_plan` ships — update it to say
  the agent now has a commit tool, while "delete" stays true (per
  `delete_plan` remaining out of scope above).
- **`agent/tools.py`** section-header comment directly above the plan-tools
  insertion point (currently ~lines 1165-1173): it asserts that plan
  activation/deletion is a human-only action performed via the REST API/UI.
  That assertion becomes false the moment `commit_training_plan` ships —
  update it to reflect the two-tier reality described in the `plans.py`
  bullet above.
- **`revise_training_plan`'s own tool description** (`agent/tools.py:1251`):
  currently reads "Cannot change a plan's status — the user commits via the
  UI." This is model-facing prompt text the agent reads on every turn — left
  unchanged, it tells the agent status changes require the UI while a
  sibling tool (`commit_training_plan`) exists specifically to change status
  directly. Because this design's entire safety guardrail is prompt-level
  wording (no code-level confirm gate — see "Out of scope"), this
  conflicting description would actively undermine that guardrail. Update it
  to drop the now-false "cannot change status" framing and instead point to
  `commit_training_plan`/`discard_training_plan_draft` as the actual path.
- **`propose_training_plan`'s own tool description** (`agent/tools.py:1215`):
  currently ends "Archives any prior draft. Does NOT activate the plan — the
  user commits it." Same class of problem as the `revise_training_plan`
  bullet above — this is model-facing prompt text, and it becomes false the
  moment `commit_training_plan` ships (the agent *can* now activate the plan
  it just proposed, when the user asks). Update it to point to
  `commit_training_plan` instead of implying the UI is the only path.
- **README.md** (four fixes, same section): line 175, "review and commit (or
  delete) a draft your coach drafted over MCP" — implies UI-only commit/delete
  under the Training Plan page description. Update the "commit" half to
  reflect that the agent can now commit a draft directly (the "delete" half
  stays accurate — draft deletion via `discard_training_plan_draft` is
  additive, and abandoning the *active* plan stays UI-only per "Out of
  scope", so "delete" in the UI sense doesn't become false). Lines 242-243, "the
  agent only drafts; committing or deleting a plan is a human action in the
  UI" (in the Write tools list under training-plan tools) — same fix: correct
  "committing ... is a human action in the UI" now that `commit_training_plan`
  exists, and note the new `discard_training_plan_draft` tool covers draft
  deletion (a genuinely new capability, not merely restating existing UI
  parity), while leaving intact that abandoning the active plan remains
  UI-only. Additionally, two more README facts are already stale today and
  need fixing in this same pass, not left for a follow-up: line ~225, "Once
  connected you get **27 tools**" — already stale (current `ALL_TOOLS` has 29
  entries as of this writing) even before this design ships; bump it to 31
  once `commit_training_plan`/`discard_training_plan_draft` are added to
  `ALL_TOOLS`. Lines ~240-243, the Write-tools training-plan enumeration
  ("the training-plan tools `propose_training_plan` / `revise_training_plan` /
  `get_training_plan_status` / `get_training_plan_progress`") already omits
  `update_plan_workout`, which is also missing from that list today — add it
  along with the two new tool names (`commit_training_plan` /
  `discard_training_plan_draft`) so the enumeration is complete and current.
  Since this same block is already being touched, fix a second pre-existing
  defect in the same pass as a low-cost freebie: `get_training_plan_status`
  and `get_training_plan_progress` are pure reads (both only call
  `plans.get_active_plan`/`load_activities_by_date`/`build_plan_status`/
  `build_plan_detail` — no DB writes anywhere in either function), so they're
  miscategorized under "Write tools" today. Move both names out of the
  Write-tools list (into "Read tools" or their own line) while making the
  enumeration edit above.
- **Release process (CLAUDE.md compliance)**: this change touches both code
  and a model-facing tool description, so per CLAUDE.md's release policy it
  requires a `pyproject.toml` version bump with a matching `CHANGELOG.md`
  entry, and a `devlog/` entry for the PR — same PR, not a follow-up.

## Out of scope (explicit, YAGNI)

- No "abandon the active plan with no replacement" agent tool. That stays
  UI-only via the existing unguarded `delete_plan`. Wiping the only active
  plan with nothing queued to replace it is a meaningfully bigger blast
  radius than draft↔draft or draft→active, and it wasn't requested.
- No `confirm: true` parameter on either tool. The guardrail is the tool
  description's usage guidance (prompt-level), the same mechanism
  `propose_training_plan`'s "ground it first" instruction already relies on.
- No scorer/A-B verification of the prompt-level guardrail. The entire
  safety property behind these tools — the agent only activates/discards a
  plan when the user has explicitly asked, in this conversation — rests on
  tool-description prose, not code. This design does not assume the
  project owner's stated policy that "prompt changes require A/B testing"
  (a standing preference communicated directly, not a rule written into
  CLAUDE.md) is scoped away from tool descriptions — that scoping was put
  to the project owner directly ("should this design's tool-description
  changes go through scorer/A-B verification, or is that rule scoped to
  brief-generation prompts only?") and confirmed by him: the policy targets
  the brief-composition generation prompts (scored via
  `score_prompt`/the `ab_brief` harness), not MCP tool descriptions, which
  are short, structured, tool-selection prose rather than generation
  prompts. This is out of scope by the project owner's own confirmed
  boundary on that policy, not a waived gap or an inference this design
  made unilaterally — consistent with the three existing plan-write tools
  (`propose_training_plan`/`revise_training_plan`/`update_plan_workout`),
  whose descriptions were never scorer/A-B-tested either.
