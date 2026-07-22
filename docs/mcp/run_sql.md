# `run_sql`

> Ad-hoc read-only SQL against the fitness SQLite DB — the escape hatch for analysis no structured tool covers. **Availability:** stdio + HTTP

## What it does

Executes a single `SELECT` (or `WITH`) statement against the fitness database on a
connection opened in SQLite's read-only URI mode, so every write and every DDL
statement fails at the engine, not at a string check. Results come back as a list
of row dicts, capped at 500 rows, with a 5-second wall-clock budget enforced by a
SQLite progress handler. It is in `ALL_TOOLS`, so both `fitness mcp-stdio` and the
networked `/mcp/` transport expose it — and it is one of the twelve tools in the
brief composer's read-only allow-list (`_READ_ONLY_TOOL_NAMES`).

**Use it last.** `CLAUDE.md` is explicit: when a structured `mcp__fitness__*` tool
fits the question, use that tool. `run_sql` exists for the residue. And never shell
out to `sqlite3` via Bash for a DB read — that has happened once and it dumped
`PRAGMA` introspection and SQL errors at the user.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | One statement. Must begin (after `.strip()`) with `select` or `with`, case-insensitive. A trailing `;` is stripped for you. Multiple statements are rejected by SQLite, not by this tool. |

## Returns

```json
{
  "rows": [
    {"date": "2026-07-18", "activity_type": "running", "distance_meters": 16093.4, "avg_hr": 141},
    {"date": "2026-07-15", "activity_type": "running", "distance_meters": 8046.7, "avg_hr": 138}
  ],
  "count": 2
}
```

`count` is `len(rows)` after truncation, **not** the number of rows the query
matched. A `count` of exactly `500` means you very likely hit the cap.

On failure the tool returns an MCP error payload (`is_error: true`) with one of a
small, deliberately non-leaky set of messages:

| Condition | Message |
|---|---|
| Doesn't start with `select`/`with` | `read-only: only SELECT/WITH queries permitted` |
| Denylisted keyword present | `forbidden keyword: <kw>` |
| Exceeded the 5s budget | `query exceeded time budget` |
| Anything else SQLite raised | `query failed: invalid query — check table/column names against the fitness://schema resource` |

That last message covers a bad column name, a bad table name, a syntax error, a
multi-statement string, and a write that slipped past the denylist and hit the
read-only connection (`attempt to write a readonly database`) — the raw SQLite
string is never surfaced.

## Example

**Ask:** "What's my average HR by day of week on runs over 6 miles this year?"

No structured tool computes a day-of-week grouping, so this is a legitimate
`run_sql` call.

```json
{
  "query": "SELECT strftime('%w', date) AS dow, COUNT(*) AS n, ROUND(AVG(avg_hr), 1) AS avg_hr FROM activities WHERE activity_type = 'running' AND distance_meters > 9656 AND date >= '2026-01-01' GROUP BY dow ORDER BY dow"
}
```

Abridged output:

```json
{
  "rows": [
    {"dow": "0", "n": 14, "avg_hr": 143.2},
    {"dow": "3", "n": 6,  "avg_hr": 148.9},
    {"dow": "6", "n": 9,  "avg_hr": 140.4}
  ],
  "count": 3
}
```

## Gotchas

- **Read-only is engine-enforced, and the keyword denylist is only
  defense-in-depth.** `db.connect_readonly()` opens `file:<path>?mode=ro` with
  `uri=True` and never commits, so `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/
  `CREATE` fail however they're phrased. Extension loading is left at SQLite's
  default (disabled), so `load_extension` is blocked too. The prefix check and the
  substring denylist (`insert `, `update `, `delete `, `drop `, `alter `,
  `create `, `attach `, `pragma `, `replace `) exist to return a clean error for
  the common case, not to be the gate.
- **The denylist matches substrings, including inside string literals.** The query
  is space-padded and scanned, so `WHERE activity_name LIKE '%create %'` is
  rejected with `forbidden keyword: create` even though it's a pure read.
  Likewise `replace (a, b, c)` written with a space after the function name is
  rejected; `replace(a, b, c)` is fine. Reformulate rather than fight it.
- **500 rows, silently.** `fetchmany(500)` truncates with no flag in the payload.
  Put your own `LIMIT`/aggregation in the query when the result set could be
  larger — `activity_hr_samples` alone holds ~1700 rows for a single run, and
  `body_battery_samples`/`stress_samples` are per-sample tables.
- **5-second wall clock.** A SQLite progress handler checks the deadline every
  10,000 VM ops and interrupts the statement, so a recursive CTE or a cartesian
  join gets `query exceeded time budget` instead of hanging the single-threaded
  server. The query runs in a worker thread (`asyncio.to_thread`), so even a
  within-budget heavy query never blocks the event loop.
- **One statement only.** `SELECT 1; SELECT 2` raises inside `sqlite3` and comes
  back as the generic `query failed` message.
- **The advertised schema is a subset of what's actually queryable.**
  `QUERYABLE_SCHEMA` in `agent/tools.py` is the single source of truth for both
  the tool description and the `fitness://schema` MCP resource, and it advertises
  eight tables:

  | Table | Grain |
  |---|---|
  | `daily_metrics` | one row per date — sleep, RHR, stress, body battery, steps, VO2 max |
  | `activities` | one row per workout (`source` = `garmin` or `manual`) |
  | `activity_splits` | per-lap; only ~87 of 747 activities have them (daily sync writes them, backfill never does) |
  | `activity_hr_zones` | seconds in each HR zone per activity |
  | `body_battery_samples` | intraday samples |
  | `stress_samples` | intraday samples |
  | `baselines` | one row per date — 60-day rolling means/SDs plus CTL/ATL/TSB |
  | `observations` | manual logs (weight, RPE, soreness, mood, free text) |

  The read-only connection can also see tables the description does *not* list:
  `activity_hr_samples`, `training_plans`, `plan_workouts`, `ingest_runs`, and
  `settings`, plus the `raw_json` columns on `daily_metrics` and `activities`.
  They're queryable but unadvertised — read the plan tables through
  `get_training_plan_progress`/`get_training_plan_status` instead, which apply the
  grading logic.
- **`fitness://schema` is the canonical column list.** Read that MCP resource
  rather than guessing; both error branches point at it for a reason.
- **Raw units, no conveniences.** Unlike `query_workouts`/`daily_snapshot`,
  nothing here is augmented — `distance_meters` is meters, `avg_pace_sec_per_km`
  is sec/km, `duration_seconds` is seconds. Nate's display units are miles and
  min/mi, so convert before showing anything.
- **Dates are TEXT, ISO `YYYY-MM-DD`.** Lexicographic comparison works
  (`WHERE date >= '2026-01-01'`); `strftime` works; there is no DATE type.

## See also

- [`query_workouts`](query_workouts.md) — filtered workout lists with mile/pace conveniences; the right tool for "what did I run last week"
- [`get_metric_trend`](get_metric_trend.md) — daily-metric series with a computed slope direction
- [`compare_periods`](compare_periods.md) / [`correlate`](correlate.md) / [`find_anomalies`](find_anomalies.md) — the statistical asks people reach for SQL to answer
- [`daily_snapshot`](daily_snapshot.md) / [`get_brief_context`](get_brief_context.md) — the assembled daily read
- [`get_training_plan_progress`](get_training_plan_progress.md) — graded plan data; do not hand-query `plan_workouts`
- [`sync_garmin_data`](sync_garmin_data.md) — the only tool that puts new rows in the tables you're querying
