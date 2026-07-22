# MCP tool reference

The MCP surface is the **only** way to interact with local-fitness — there is no
web UI. This directory documents every tool, one page each.

Start here to find the right tool; follow the link for parameters, return shape,
worked examples, and gotchas.

- **[Connecting](#connecting)**
- **[Availability: stdio vs HTTP](#availability-stdio-vs-http)**
- **[Tools by area](#tools-by-area)**
- **[Prompts and resources](#prompts-and-resources)**
- **[Conventions](#conventions)**

---

## Connecting

**Local, over stdio** — all 37 tools, no token:

```bash
claude mcp add --transport stdio fitness -- uv run fitness mcp-stdio
```

**Over the running server** — 35 tools, bearer-gated:

```bash
claude mcp add --transport http fitness \
  https://<your-host>/mcp/ --header "Authorization: Bearer $TOKEN"
```

See the [Authentication](../../README.md#authentication) section for the token,
and [`docs/deployment.md`](../deployment.md) for the container setup.

## Availability: stdio vs HTTP

Two tools are **local-only** — registered by `run_stdio()` and structurally
unreachable over the networked `/mcp/` transport:

| | stdio (`fitness mcp-stdio`) | HTTP (`/mcp/`) |
|---|---|---|
| Tool count | **37** | **35** |
| [`generate_brief_report`](generate_brief_report.md) | ✅ | ❌ |
| [`workout_report_card`](workout_report_card.md) | ✅ | ❌ |

**The rule that decides membership:** a tool that hands back a *filesystem path*
is local-only, because a remote caller receives a container-internal path it
cannot retrieve. [`generate_chart`](generate_chart.md) is networked precisely
because it returns the PNG as an inline MCP image content block — the client no
longer needs the path.

## Tools by area

### Status and snapshots

Three tools overlap here; the distinction is on each page.

| Tool | Use it for |
|---|---|
| [`daily_snapshot`](daily_snapshot.md) | One-call "how am I doing today" |
| [`get_today_status`](get_today_status.md) | Today's metrics vs baseline + training load, no plan/trend data |
| [`get_brief_context`](get_brief_context.md) | The deterministic planner's full typed output — candidate takeaways, plan status, anomalies, continuity |
| [`save_brief`](save_brief.md) | ✍️ Persist today's composed brief |

### Metrics and trends

| Tool | Use it for |
|---|---|
| [`get_metric`](get_metric.md) | Raw daily values for one metric over N days |
| [`get_metric_trend`](get_metric_trend.md) | The same series with a computed direction and slope |
| [`training_load_status`](training_load_status.md) | CTL / ATL / TSB — fitness, fatigue, freshness — with a zone read |

### Workouts

| Tool | Use it for |
|---|---|
| [`query_workouts`](query_workouts.md) | List/filter sessions |
| [`get_workout_detail`](get_workout_detail.md) | One session in full, including splits where they exist |
| [`workout_report_card`](workout_report_card.md) | 📄 **Graded** report card for one session — letters, not adjectives |
| [`log_manual_workout`](log_manual_workout.md) | ✍️ Record a non-Garmin session (feeds CTL/ATL/TSB) |
| [`delete_manual_workout`](delete_manual_workout.md) | ✍️ Remove one |

### Analysis

The first three attach **deterministic interpretation** — see
[Conventions](#conventions).

| Tool | Use it for | Interpretation attached |
|---|---|---|
| [`compare_periods`](compare_periods.md) | Two windows side by side | `cohens_d`, `magnitude`, `delta_pct` |
| [`correlate`](correlate.md) | Relationship between two metrics | `strength`, `direction` (min 5 pairs) |
| [`find_anomalies`](find_anomalies.md) | Outlier days | `sd_distance`, direction |
| [`recovery_pattern`](recovery_pattern.md) | How you rebound after hard days | **none** — its determinism is hard-coded baseline thresholds, not `interpret.py` |

### Training plans

The full lifecycle. These eight are a state machine — every page carries the
diagram.

```
propose_training_plan ──> DRAFT ──> commit_training_plan ──> ACTIVE
                            │  ▲                                │
              revise_training_plan                   update_plan_workout
                            │                                   │
                discard_training_plan_draft         abandon_active_plan (no undo)
```

| Tool | Use it for |
|---|---|
| [`propose_training_plan`](propose_training_plan.md) | ✍️ Draft a new plan toward a goal |
| [`revise_training_plan`](revise_training_plan.md) | ✍️ Restructure the draft |
| [`commit_training_plan`](commit_training_plan.md) | ✍️ Activate the draft |
| [`discard_training_plan_draft`](discard_training_plan_draft.md) | ✍️ Throw the draft away |
| [`update_plan_workout`](update_plan_workout.md) | ✍️ Re-prescribe **one day** on the active plan |
| [`abandon_active_plan`](abandon_active_plan.md) | ✍️ Stop following the active plan — **no undo** |
| [`get_training_plan_status`](get_training_plan_status.md) | Is there a plan, and what's today? |
| [`get_training_plan_progress`](get_training_plan_progress.md) | Day-by-day grading + adherence rollups |
| [`plan_chart`](plan_chart.md) | Scheduled vs actual, rendered |

### Charts and reports

| Tool | Use it for |
|---|---|
| [`chart`](chart.md) | ASCII/emoji chart, inline in the reply |
| [`generate_chart`](generate_chart.md) | Standalone matplotlib PNG, returned inline |
| [`plan_chart`](plan_chart.md) | **The** tool for scheduled-vs-actual — never hand-roll this |
| [`generate_brief_report`](generate_brief_report.md) | 📄 Render a saved brief to a PRESS-themed PDF |
| [`workout_report_card`](workout_report_card.md) | 📄 Graded single-workout card, markdown + PDF |

### Preferences and subjective data

Two different things — see each page.

| Tool | Use it for |
|---|---|
| [`save_user_note`](save_user_note.md) | ✍️ A durable **coaching preference** (shapes every future reply) |
| [`list_user_notes`](list_user_notes.md) | Read them back |
| [`update_user_note`](update_user_note.md) | ✍️ Amend one |
| [`delete_user_note`](delete_user_note.md) | ✍️ Remove one |
| [`log_observation`](log_observation.md) | ✍️ A timestamped **data point** — RPE, soreness, weight, mood… |
| [`list_observations`](list_observations.md) | Read them back |
| [`delete_observation`](delete_observation.md) | ✍️ Remove one |

### Data and escape hatches

| Tool | Use it for |
|---|---|
| [`sync_garmin_data`](sync_garmin_data.md) | ✍️🌐 Trigger a capped Garmin pull + baseline recompute |
| [`run_sql`](run_sql.md) | Read-only SQL — **last resort**, when no structured tool fits |

**Legend** — ✍️ writes · 🌐 reaches the network · 📄 stdio-only (returns a file path)

## Prompts and resources

**Prompts (2)**

| Prompt | What it does |
|---|---|
| `coach` | Assembles the full daily snapshot *plus* the coach persona and your saved preferences in one round-trip, then stays conversational. The everyday entry point. |
| `brief` | Composes a fresh structured daily brief from the same snapshot and persists it via [`save_brief`](save_brief.md). |

**Resources (2)**

| URI | Contents |
|---|---|
| `fitness://schema` | Queryable tables and columns, plus the read-only SQL guide — read this before reaching for [`run_sql`](run_sql.md). |
| `fitness://brief/latest` | Your most recent brief as Markdown. Leads with a **STALE** banner when serving a brief older than today. |

## Conventions

These hold across the whole surface.

**Numbers are never invented.** The agent must call a tool to read a real value
before any claim. The server itself runs no Claude inference — it hosts
deterministic compute over MCP, and all synthesis happens in the client agent.

**Judgments are computed, not phrased into existence.** `agent/interpret.py` is
a pure, stdlib-only module holding every classifier — `tsb_zone`, `pct_change`,
`trend_direction`, `delta_direction`, `baseline_position`, `correlation_read`,
`effect_size`, `sd_position`. Analysis tools attach these fields to their
payloads rather than leaving the model to apply a legend by hand. The rule is
repo-wide: **the LLM phrases a judgment, it never derives one tested Python can
compute.**

**Display units are miles.** Distances and pace render as `mi` and `min/mi`
alongside the raw SI values (`LOCAL_FITNESS_DISPLAY_UNITS`).

**Charts render in the reply.** When a tool produces a chart, paste the full
output into the message in a fenced code block. A chart left in a collapsed tool
call forces a Ctrl-O to see it.

**Prefer a structured tool over `run_sql`.** There is a purpose-built tool for
almost every question. Never shell out to `sqlite3` for a DB read.

**Write tools are real.** Anything marked ✍️ mutates local state. The scheduled
brief composer is restricted to a read-only allow-list, so an automated run can
never mutate your data — but an interactive client can.

## See also

- [Project README](../../README.md)
- [`docs/deployment.md`](../deployment.md) — container and reverse-proxy setup
- [`CHANGELOG.md`](../../CHANGELOG.md)
