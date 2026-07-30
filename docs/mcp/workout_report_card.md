# `workout_report_card`

> Graded report card for ONE workout — three compliance grades plus an overall, an ungraded training-stimulus report, a coach's read, and a PRESS-themed PDF. **Availability:** stdio only — local

## What it does

Answers "how did that run go", "was that any good", "grade my workout". It
grades one activity on three compliance metrics — distance, pace, HR — in
deterministic Python, reports training stimulus separately and ungraded, then
has the coach *phrase* those grades rather than derive them. Use it instead of [`get_workout_detail`](get_workout_detail.md)
whenever the question is a judgment: `get_workout_detail` reports columns, this
one renders a verdict with a named yardstick. Use
[`plan_chart`](plan_chart.md) instead when the question is about the plan as a
whole rather than one session.

The returned `markdown` field is already formatted. **Render it to the user
verbatim.** Do not re-summarize it, do not rebuild your own verdict from the
structured fields, and do not assemble a card by hand out of
`get_workout_detail` when a graded one exists.

### Why this one is local-only

The rule: **a tool that hands back a filesystem path is local-only**, because a
remote `/mcp/` caller receives a container-internal path it cannot retrieve. A
PDF cannot ride back as an MCP content block the way `generate_chart`'s PNG
does, so there is no networked escape hatch. This tool sits in
`LOCAL_ONLY_TOOLS` and is registered **only** by `run_stdio()`'s
`build_server(extra_tools=LOCAL_ONLY_TOOLS)`; `build_session_manager()` calls
`build_server()` argument-free, so the authenticated `/mcp/` transport
structurally cannot see it.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_id` | integer | no | — | Grade this exact activity. **Overrides `date`.** Bypasses the `distance_meters > 0` filter, so a strength session can be requested explicitly and comes back with `n/a` where metrics are missing. |
| `date` | string | no | — | `YYYY-MM-DD`; grades that day's primary session — the **first** `start_time` with distance and duration > 0. First, not last, because the prescription it gets graded against is the day's lowest `seq`, i.e. the morning session; taking the last one paired an evening shakeout with the morning's long-run target. Malformed dates error before any DB access. |
| `format` | string | no | `both` | `both` \| `table` \| `pdf`. `table` skips the PDF **and** skips the on-demand HR-trace fetch, keeping the call purely local and fast. |

With neither `activity_id` nor `date`, it grades the most recent logged activity
with distance and duration.

## The rubric

**Two surfaces: compliance is graded, stimulus is only reported** (0.40.0).

| Surface | Metrics | Output |
|---|---|---|
| **Compliance** — "did you execute the prescription?" | distance, pace, HR | letter grades + the overall |
| **Stimulus** — "what did this run do to your body?" | training load, aerobic/anaerobic TE, HR-zone share, drift | numbers + a `LOW`/`MODERATE`/`HIGH`/`VERY HIGH` descriptor, **never a letter** |

Grading load *and* HR graded one variable twice with the sign reversed: Garmin's
training load is essentially `duration x f(HR)`, so obeying an easy day's HR cap
mechanically drove the load number down, and load's undershoot penalty then
punished the compliance the HR grade had just rewarded. Measured 2026-07-29, two
easy days under the same prescription ("Easy 5mi. Keep HR under 140."): the run
that followed it exactly scored **C** — a 3.60-GPA A destroyed by the F-cap on
load — while the one that blew the cap from mile 3 scored **A**, earning A+ on
load for the extra work. The rubric was inverted, not merely blunt.

Load is absent from every `INTENT_METRIC_WEIGHTS` table, so "load cannot lower
your grade" is structural rather than a small weight a cap could bypass.

**Each compliance metric reduces to a single non-negative relative deviation
`d`**, and every `d` goes through the same `GRADE_BANDS`. Three small deviation
functions, one grader — that is what keeps the rubric testable.

| `d` ≤ | Grade |
|---|---|
| 0.05 | A |
| 0.10 | B |
| 0.20 | C |
| 0.35 | D |
| above | F |

A `+` / `-` modifier marks position within the band (bottom third earns the
`+`). Modifiers are cosmetic: GPA math runs on base letters only.

Two band-width multipliers exist:

- **`PLAN_TIGHTEN` = 0.6** on plan-referenced distance and pace. A plan target is
  an *instruction*; a rolling median is a *reference*, and holding both to the
  same tolerance let a prescribed 10:28 easy run executed at 9:28 score a B-,
  which in turn let the card print an overall A for a run its own read called
  "you never ran easy at all." Tightened bands are A ≤3%, B ≤6%, C ≤12%, D ≤21%.
- **`STEADY_WIDEN` = 1.5** on steady/unknown-intent pace against the rolling
  median, which has no stated target to be held tightly against.

### Two reference modes — the card always says which

| Mode | What it is |
|---|---|
| `plan` | The active plan's prescribed workout for that date — distance, pace, and **HR when the day sets `target_hr_max`** (0.40.0). Without a stated cap HR falls back to the rolling band; training load has no plan column and is no longer graded at all. A `rest` prescription is an intent signal only; its null targets never grade anything. |
| `rolling_60d` | Trailing-60-day **medians** over comparable activities, computed on the fly. Median, not mean, because the history carries real training-load outliers. Not the `baselines` table — that holds no per-workout aggregates at all. |
| `insufficient_data` | Fewer than `MIN_REFERENCE_ACTIVITIES` (**5**) comparable activities even after widening. Grading against noise is worse than not grading: the affected metrics return n/a and the card says so. **The two modes compose** — a thin rolling pool does not stop the plan from grading distance and pace, so the disclaimer is scoped to the metrics it applies to ("HR ungraded — only 2 comparable activities…") instead of printing a blanket "not enough comparable history to grade" under two letters the plan just graded. |

The rolling window **ends the day before** the graded activity, so a workout can
never move its own goalposts.

**Comparability is exact `activity_type` first**, widening to the on-foot class
only when the exact pool is too thin — and the widening is disclosed on the
card. Measured on live data: pooling `running` with `treadmill_running` put
median HR at 119 against an outdoor average of 140 and gave a normal easy run a
D. Treadmill and road are different HR regimes.

### Direction gating

An easy run is *supposed* to be slow; grading `|actual − expected|` would hand
every recovery run an F.

| Metric | Penalized for |
|---|---|
| Pace, easy / long | too FAST only. Slower than the easy expectation is the point, and scores an A. |
| Pace, quality (tempo/interval/race) | too SLOW only. Beating the target is an A, uncapped. |
| Pace, steady | both directions, bands widened by `STEADY_WIDEN`. |
| Distance vs plan | both directions — a 12-miler on a 10-mile prescription is over-cooking the plan. |
| Distance vs rolling median | short only. Going longer than your norm is never a penalty. |
| HR vs a prescribed `target_hr_max` | over the cap only — the worse of *average over cap* and *fraction of split time over it past `HR_CAP_GRACE_FRACTION` (5%)*. Graded on the BASE bands, not `PLAN_TIGHTEN`: tightening a time fraction would double-count strictness. |
| HR vs the rolling band | outside the intent's band only; inside is a flat 0.0 (an A). HR is judged on appropriateness, never "lower is better". |
| Load | **not graded** (0.40.0). The deviation is still computed for display and drives the stimulus descriptor; `LOAD_SPIKE_FACTOR` (2.0×) now decides `as_intended` and the spike flag instead of a letter. |

Each expectation is intent-scaled off the rolling median (a plan states its own
targets and needs no scaling):

| Intent class | `DISTANCE_FACTORS` / `LOAD_FACTORS` | `PACE_FACTORS` | `HR_BANDS` (× median HR) — used only when the plan states no cap |
|---|---|---|---|
| easy | 0.75 / **0.61** | 1.10 | ≤ 0.97 |
| long | 1.40 | 1.05 | ≤ 1.00 |
| quality | 1.00 | 0.95 | ≥ 1.00 |
| steady | 1.00 | 1.00 | 0.93 – 1.07 |

Intent comes from the plan's prescribed `type` when there is one
(`intent_source: "plan"`), otherwise it is inferred from the run's own numbers
(`"inferred"`) — distance is checked before HR, because a long run is typically
run at easy HR and mis-classing it "easy" would grade it against a 0.75× distance
expectation and hand it an automatic A.

### The overall grade

Intent-weighted GPA over gradeable metrics only. Flat weights let the two
lowest-information metrics outvote the point of the session — the same
prescribed-10:28-run-at-9:28 scored an overall B because HR and load together
carried 40% and both landed A.

| Intent | distance | pace | hr | load |
|---|---|---|---|---|
| easy | 0.20 | **0.45** | 0.25 | 0.10 |
| quality | 0.20 | **0.45** | 0.25 | 0.10 |
| long | **0.45** | 0.20 | 0.20 | 0.15 |
| steady | 0.30 | 0.30 | 0.25 | 0.15 |

An `n/a` metric drops out and its weight redistributes proportionally. Zero
gradeable metrics yields `"n/a"` — never `F`, which would read as a judgment
that wasn't made. GPA cuts: ≥3.5 A, ≥2.5 B, ≥1.5 C, ≥0.5 D, else F.

**The F floor.** An F on any single metric caps the overall at **C**, reported
as `"capped_by": "F"` rather than silently rewritten — a card printing "Overall:
A" above a row reading F is not reporting a grade, it is averaging away the
finding.

## Returns

A single text content block holding this object. `markdown` is the payload;
everything else is metadata for the caller's own logic.

```json
{
  "markdown": "# Report Card — Interval Session\n**2026-07-21** · 5.95 mi in 1:03:41 · 10:42/mi\n\n## Overall: D (1.05 GPA)\n…",
  "activity_id": 23685126977,
  "date": "2026-07-21",
  "overall": {"grade": "D", "gpa": 1.05, "graded_metrics": 4},
  "grades": {"distance": "D-", "pace": "F", "hr": "B+", "load": "D+"},
  "reference": "rolling_60d",
  "intent": "interval",
  "intent_source": "plan",
  "splits_available": true,
  "path": "/var/folders/.../local-fitness-reports-48213-x9f2a1/report-card-23685126977.pdf"
}
```

| Field | Notes |
|---|---|
| `markdown` | The formatted card. **Render verbatim.** |
| `overall` | `{grade, gpa, graded_metrics}`, plus `capped_by: "F"` when the F floor fired. `gpa` is `null` and `grade` is `"n/a"` when nothing was gradeable. |
| `grades` | Per-metric letter, or `null` for an ungraded metric. |
| `reference` | The reference *mode* — `plan` never appears here; this is `rolling_60d` or `insufficient_data`, since the mode belongs to the rolling pool. Whether the plan supplied targets is visible in `markdown`'s reference line. |
| `intent` / `intent_source` | e.g. `"interval"` / `"plan"`, or `"easy"` / `"inferred"`. |
| `splits_available` | False for the ~88% of history that was backfilled. |
| `other_activities_on_date` | Present only on a double day, so the other session isn't silently hidden. Each entry is `{activity_id, activity_type, distance_mi, start_time}` — enough to say *which* session went ungraded, which a bare id could not. |
| `path` | Present unless `format="table"`. |

Errors: `no matching activity found` (with the `activity_id` / `date` echoed),
`malformed date '...'`, `unknown format '...'`, `PDF render failed: ...`.

### The markdown card (abridged, real numbers from 2026-07-21)

```markdown
# Report Card — Interval Session
**2026-07-21** · 5.95 mi in 1:03:41 · 10:42/mi

## Overall: D (1.05 GPA)

**Distance** — You went 5.95 against a prescribed 5. …
**Pace** — Your best mile was 9:25 against a 6:58 rep target. …
**Heart Rate** — 136 against a 145 floor for a quality day. …
**Training Load** — 81 banked against an interval-day expectation of 105. …

| Metric | Actual | Expected | Delta | Grade |
| --- | --- | --- | --- | --- |
| Distance | 5.95 mi | 5.00 mi | +19% | D- |
| Pace | 9:25/mi best mile | 6:58/mi | 147s/mi slower | F |
| Avg HR | 136 bpm | ≥ 145 bpm | -6% | B+ |
| Training Load | 81 | 105 | -23% | D+ |

_Graded against your **training plan** for this date (intent: interval,
prescribed by your plan). HR and training load have no plan target, so they use
your 60-day median of 16 treadmill_running activities. 30 same-window
walking-effort activities excluded — Garmin labels them the same, the pace says
otherwise._

## Per-mile breakdown

| Mile | Pace | Avg HR | vs run | Elev |
| --- | --- | --- | --- | --- |
| Mile 1 | 11:58/mi | 121 bpm | -15 | 4 m |
| …
```

The four labelled paragraphs under the grade line are the coach's read
(`agent/workout_coach.py`). They are told the grades are not theirs to revise,
and are forbidden from naming a letter — the letters print in the table
immediately below.

## Example

**Ask:** "how did this morning's run go?"

```
workout_report_card()
```

→ grades the most recent activity, writes
`report-card-<activity_id>.pdf`, opens it on the Mac, and returns the payload.
Paste `markdown` into the reply as-is and stop there. If the user wants the
table only and not the PDF, `workout_report_card(format="table")`.

## Gotchas

- **Render `markdown` verbatim.** It already carries the grade line, the coach's
  four paragraphs, the metric table, the notes, the yardstick sentence and the
  split table. Re-summarizing it throws away the one thing this tool exists to
  produce — a stable verdict.
- **The reference pool is gated on MEASURED locomotion, not the Garmin label.**
  `activity_type` is not trustworthy: a walking-desk session logs as
  `treadmill_running`, and both the exact-type filter and `plans._is_running` (a
  substring match) pass it straight through. `RUN_PACE_CEILING_SEC_PER_MI` — a
  13:00 mile — partitions the pool on the data before any type filter runs, so a
  run only ever compares against running-effort activities. Measured 2026-07-21:
  without it, the "median comparable activity" was a 15:50/mi walk at 116 bpm
  and 22 load, which handed a genuine interval session A+ on both HR and load
  for clearing a bar set by walking. A paceless row has an unknown mode and
  joins neither pool. The count of excluded rows is disclosed in the card's
  reference line.
- **Quality-day pace is graded on the FASTEST REP-SIZED SPLIT** — the single
  documented exception to "no grade reads `activity_splits`". A plan's interval
  pace describes the *reps*, while `avg_pace_sec_per_km` averages in the warmup,
  the recovery jogs and the cooldown; comparing them is not a strict rubric but
  an arithmetic guarantee of an F for every correctly-executed session. Where no
  split qualifies it returns **n/a with a stated reason** and its weight
  redistributes — it never falls back to the comparison it exists to avoid.
- **Rep-sized means `distance_meters >= QUALITY_MIN_SPLIT_M` (300 m), not "not
  partial".** `label_splits` calls a split partial relative to the workout's own
  *longest* lap, and on a manually-lapped session the longest lap is the warmup:
  a 1600 m warmup followed by 800 m reps marks every rep partial, leaving the
  warmup as the only "full" split — so the reps were graded at warmup pace, a
  guaranteed F on exactly the sessions this exception exists to grade fairly. The
  floor still solves what the partial filter was there for (a 90-metre trailing
  fragment posts an absurd pace and would win every time), and it sits under a
  standard 400 m rep. A slower warmup simply loses `min()`. When the graded split
  is shorter than the workout's full lap the card says "best split" rather than
  "best mile" — the label has to match what was actually measured.
- **Everything else about splits is presentation-only.** Only 87 of 747
  activities have them — the daily-sync ingest path writes them, backfill never
  did — so a splits-dependent grade would be unavailable on ~88% of history and
  would quietly mean different things on different rows. The per-sample HR trace
  and the HR-drift line are the same: printed, never graded.
- **A split-heavy card (6+ splits) renders as 2 PDF pages.** Known layout
  limitation, not a bug, and it predates the coach read. The card sits right on
  the page boundary and the read's word count is the swing factor — which is why
  the 45-words-per-paragraph budget is real rather than cosmetic. Measure any
  added content with `len(HTML(...).render().pages)` against a 6-split activity
  before shipping it.
- **`format="both"` and `format="pdf"` return the same payload** — `markdown` is
  always included. `format="table"` is the meaningful switch: it drops `path`
  *and* skips the on-demand HR-trace fetch, which is the only branch of this tool
  that can touch the network.
- **The PDF path may call Garmin.** `load_report_card_inputs(hr_trace=True)`
  resolves ~1700 per-sample HR readings for the one activity being graded and
  caches them forever in `activity_hr_samples`. Never backfilled — 747
  activities of detail calls is exactly the shape that trips Garmin's 429. Every
  failure mode (missing credential, expired token, no HR channel, pre-0.25.0 DB)
  returns "no samples" and the chart falls back to per-lap; a failed fetch caches
  nothing.
- **The coach read is a Claude call and can degrade.** `claude-sonnet-5`,
  `effort=low`, thinking disabled, 90s timeout, behind a single-entry disk cache
  keyed on the pure prompt hash **plus `activity_id`** (without the id, a double
  day with identical names and grades served the first card's read for the
  second). A generation missing any of the four labelled sections raises, is
  never cached, and drops to the deterministic `fallback_read` template — the
  card's grades are unaffected either way, since every letter was computed in
  Python first.
- **WeasyPrint needs native Pango/HarfBuzz.** On macOS: `brew install pango`
  then `export DYLD_LIBRARY_PATH="$(brew --prefix)/lib"` (or put it in `.env`).
  `format="table"` needs none of this.
- **Output location and theming.** Default is a per-process ephemeral
  `tempfile.mkdtemp()` directory, auto-opened on macOS and cleaned up at exit;
  `LOCAL_FITNESS_REPORTS_DIR` opts into a persistent one. Styling is the PRESS
  brand theme, deep-merge-overridable via `LOCAL_FITNESS_BRAND_FILE` — only D and
  F take the accent, everything at C or better stays ink.
- **HR's `Expected` column is a band, not the median.** HR is the one metric held
  to a *range*, so it carries `expected_display` (e.g. `≥ 145 bpm`), `band` and
  `in_band`, and a run inside the band reads `in range` rather than a percentage
  against one edge. Showing the bare median here once printed "-7%" beside a B+
  when the finding was 6% *above* the ceiling that produced the grade.
- **Before changing `HR_BANDS`, check the proposed bound against the real
  distribution.** The original easy ceiling (0.88×) demanded a number that
  appeared in 1 of 13 runs in the window, making HR a standing penalty rather
  than a judgment. The reference median is taken over ALL comparable
  activities, which for a mostly-easy runner already sits near easy HR.

## See also

- [`get_workout_detail`](get_workout_detail.md) — the same activity, reported
  rather than judged.
- [`query_workouts`](query_workouts.md) — find the `activity_id`.
- [`plan_chart`](plan_chart.md) — adherence across the plan instead of one
  session.
- [`generate_brief_report`](generate_brief_report.md) — the other PDF tool, and
  the other member of `LOCAL_ONLY_TOOLS`.
