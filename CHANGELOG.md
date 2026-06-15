# Changelog

All notable changes to local-fitness are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-15

### Added
- **Training plans.** A new `/plan` tab where you pick a goal (5K / 10K / Half /
  Full / Custom), a race date, and a target time; the agent drafts a periodized
  plan from your Garmin history, you riff with it in chat, and commit it. The
  committed plan is tracked on the tab (goal header with a computed Riegel
  predicted finish, schedule with per-day adherence verdicts, planned-vs-actual
  weekly mileage, CTL trajectory) and folded into the daily brief's workout
  takeaway (yesterday's adherence + today's session, with recovery taking
  precedence over the schedule on red-flag days).
- Two new tables (`training_plans`, `plan_workouts`) with a partial unique index
  enforcing a single active plan at the DB level.
- Three draft-only agent tools (`propose_training_plan`, `revise_training_plan`,
  `get_training_plan_status`) — the agent can only write drafts; activating or
  deleting a plan is a human action.
- REST: `GET /api/plan`, `POST /api/plan/{id}/commit`, `DELETE /api/plan/{id}`.
- `plans.score_plan` — a deterministic plan-quality gate (safe ≤15%/week ramp +
  taper into the race).
- `scripts/ab_brief.py` — an A/B simulation harness for prompt changes:
  generates the brief across models and flags structural divergences (takeaway
  count, mandated steps takeaway, plan-folded-in, tones). Dry-run by default
  with a hard generation cap and a cost-free `--mock` mode.
- A `Content-Security-Policy` header (`script-src 'self'`) as defense-in-depth
  against XSS from AI-authored plan strings.
- The plan schedule shows **Target** and **Actual** columns (distance + pace);
  past days where you fell short on distance or pace render red. All
  runner-facing distances/pace display in **miles** (mi, min/mi).
- The Today tab shows a **Today's Goal** card (mileage + pace to hit) whenever
  the active plan prescribes a session today — read deterministically from
  `/api/plan`, not the LLM brief.

### Security
- The whole feature was hardened through a siege + red-team adversarial pass
  before implementation; the design and contract live in `docs/plans/`. Adherence
  is computed from the activities join (immune to plan-row edits), graded against
  the data frontier (Garmin lag never shows a false "missed"), and type-aware
  (intervals/tempo by duration, cross-training by non-running activity).

## [0.1.0] - 2026-06-06

First documented release. The version was already `0.1.0` in `pyproject.toml`;
this entry inaugurates the changelog and adds the "treat the agent as code"
quality infra. No runtime/app behaviour changed — these are dev-side guardrails,
so the version is documented rather than bumped.

### Added
- `scripts/score_prompt.py` — an eval that scores `agent/prompts.py` against
  grounded pass/fail checks (never-fabricate rule, CTL/ATL/TSB translation,
  roast-when-slipping tone, MCP-tool references, user-notes injection, the
  briefing schema-lock) and exits non-zero on failure so CI can gate on it.
  Its highest-value check cross-validates that every metric/tone the briefing
  prompt advertises is a member of the `Tone`/`MetricName` enums in
  `agent/schemas.py` — catching prompt↔schema drift that would otherwise break
  briefs silently.
- `tests/` — pytest suite covering the deterministic, network-free core
  (`db`, `notes`, `agent/schemas`, `agent/prompts`, `ingest/baselines`, the
  `agent/tools` handlers, and the scorer). `pyproject.toml` enforces a
  whole-repo coverage gate via `--cov-fail-under` (floor 43%; actual ~46%).
  The Garmin-ingest, Claude chat/briefing, and FastAPI-route layers are
  largely excluded from exercise by design (network/SDK).
- Made `tests/test_security.py` hermetic: the auth/route cases were silently
  depending on a developer's real `data/fitness.db` and only failed once CI
  ran them on a fresh clone (`no such table: daily_metrics`). They now run
  against a schema-initialized temp DB.
- `.github/workflows/ci.yml` — runs ruff, the test suite with the coverage
  gate, and the prompt scorer on every push and PR to `master` (uv toolchain).
- `.github/workflows/release.yml` — after CI is green on `master`, cuts a
  GitHub Release + tag for the `pyproject.toml` version if it isn't already
  released (idempotent, notes pulled from this changelog). Bumping the version
  is what ships a release; a normal merge is a no-op.
- `requirements`/dev deps: `pytest-cov` and `coverage` added to the dev group.
- This `CHANGELOG.md`.
