# `workout_report_card`

> Rated report card for ONE workout — four compliance star ratings plus an overall, an unrated training-stimulus report, a coach's read, an inline HR image, and an optional PRESS-themed PDF. **Availability:** stdio + HTTP

## What it does

Answers "how did that run go", "was that any good", "grade my workout". It
grades one activity on four compliance metrics — distance, pace, HR, continuity
— in deterministic Python, reports training stimulus separately and ungraded,
then has the coach *phrase* those grades rather than derive them. Use it instead of [`get_workout_detail`](get_workout_detail.md)
whenever the question is a judgment: `get_workout_detail` reports columns, this
one renders a verdict with a named yardstick. Use
[`plan_chart`](plan_chart.md) instead when the question is about the plan as a
whole rather than one session.

The returned report text (`content` by default; `markdown` in JSON exports) is
already formatted. **Render it to the user verbatim.** Do not re-summarize it, do not rebuild your own verdict from the
structured fields, and do not assemble a card by hand out of
`get_workout_detail` when a graded one exists.

### In Codex, ChatGPT and other MCP clients

The default `format="inline"` returns formatted report text plus a PNG image
when stored HR data is available. Display both. Grades are deterministic; the
coaching text reuses a matching cached read or uses a labeled computed summary.
The immediate path never calls a model, reflects into the journal, or fetches
Garmin data. Missing splits and absent sync coverage are surfaced with guidance.
These coverage dates do not certify that every Garmin endpoint succeeded.

Request `format="pdf"` to export. Over HTTP, set `LOCAL_FITNESS_PUBLIC_URL` to a
trusted, browser-reachable HTTP(S) origin: the response contains a download link
valid for up to ten minutes. Links authorize only one PDF, contain no API token,
and can expire early on restart or storage eviction (32 files / 32 MiB total).
Without this setting, PDF export returns guidance to use inline instead.
Over stdio, explicit PDF exports retain local save-and-open behavior.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_id` | integer | no | — | Grade this exact activity. **Overrides `date`.** Bypasses the `distance_meters > 0` filter, so a strength session can be requested explicitly and comes back with `n/a` where metrics are missing. |
| `date` | string | no | — | `YYYY-MM-DD`; grades that day's primary session — the **first** `start_time` with distance and duration > 0. First, not last, because the prescription it gets graded against is the day's lowest `seq`, i.e. the morning session; taking the last one paired an evening shakeout with the morning's long-run target. Malformed dates error before any DB access. |
| `format` | string | no | `inline` | `inline`: text + HR image; `table`: JSON/Markdown; `pdf` or `both`: explicit PDF export. |
| `coaching` | string | no | `cached` for inline/HTTP | `cached`: matching stored read or computed summary. `generate`: local-only model generation on a cache miss; default for local table/PDF exports. |

With neither `activity_id` nor `date`, it grades the most recent logged activity
with distance and duration.

## The rubric

**Two surfaces: compliance is graded, stimulus is only reported** (0.40.0).

| Surface | Metrics | Output |
|---|---|---|
| **Compliance** — "did you execute the prescription?" | distance, pace, HR, continuity | 1-5 star ratings + the overall |
| **Stimulus** — "what did this run do to your body?" | training load, aerobic/anaerobic TE, HR-zone share, drift | numbers + a `LOW`/`MODERATE`/`HIGH`/`VERY HIGH` descriptor, **never a rating** |

**5 stars means the day was executed as prescribed.** It is a COMPLIANCE score,
not a verdict on how good the run was — a correctly-run easy day rates 5 and is
by design a low-stimulus day. Those are different claims, and conflating them is
exactly what inverted the rubric in 0.40.0 (below).

Grading load *and* HR graded one variable twice with the sign reversed: Garmin's
training load is essentially `duration x f(HR)`, so obeying an easy day's HR cap
mechanically drove the load number down, and load's undershoot penalty then
punished the compliance the HR grade had just rewarded. Measured 2026-07-29, two
easy days under the same prescription ("Easy 5mi. Keep HR under 140."): the run
that followed it exactly scored **C** — a 3.60-GPA A destroyed by the F-cap on
load — while the one that blew the cap from mile 3 scored **A**, earning A+ on
load for the extra work. The rubric was inverted, not merely blunt.

Load is absent from every `INTENT_METRIC_WEIGHTS` table, so "load cannot lower
your rating" is structural rather than a small weight a cap could bypass.

**Each compliance metric reduces to a single non-negative relative deviation
`d`**, and every `d` goes through the same curve. Four small deviation
functions, one grader — that is what keeps the rubric testable.

Since 0.50.0 that curve is continuous, mapping `d` to a score in **[1.00, 5.00]**
with fractional precision, and it saturates at both ends rather than
extrapolating:

| `d` | Stars |
|---|---|
| 0.00 | 5.00 |
| 0.05 | 4.00 |
| 0.10 | 3.00 |
| 0.20 | 2.00 |
| 0.35 and above | 1.00 |

Between those knots the score interpolates linearly, so sub-band position is
information rather than a cosmetic `+`/`-`.

**Those knots ARE the old letter bands, deliberately.** `STAR_KNOTS` is the
retired `GRADE_BANDS` with the letters removed, so every boundary the rubric was
calibrated against still sits where it sat — in particular `d = 0.35` was the F
floor and is now exactly `STAR_FLOOR`, which is what keeps `HR_CAP_BPM_SCALE`
putting the bottom of the scale at 11.3 bpm sustained over a prescribed cap.

Why the change: the letter rubric could only say which of five buckets a run
fell in, and the buckets were badly unbalanced. Measured over 240 real cards,
the `+`/`-` modifier was 545 of 749 graded rows (73%), A+ alone was 63% of
distance rows and 90% of HR rows, and a quarter of all cards scored a perfect
4.00 GPA. Continuous scoring took the overall from 4 occupied levels to 12 and
halved the share of perfect scores.

The glyphs quantize to a **quarter star** and the numeral always prints beside
them. A quarter, not a tenth: one quarter star spans 0.063-0.19 mi on a 5-mile
expectation against a 0.02 mi measurement floor, while a tenth would move a
visible step for a difference the data cannot resolve. A partial star never
rounds up to a full one and a full star is never faked, so `4.99` draws
`★★★★¾ 4.99`.

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
| Continuity | slow only, and only the SLOWEST full split against the run's own median, past `CONTINUITY_TOLERANCE` (1.15). Answers "was this one session, or did it contain a break?" — the question distance, pace and HR all average away. |
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

### The overall rating

Intent-weighted mean of the per-metric scores. Flat weights let the two
lowest-information metrics outvote the point of the session — the same
prescribed-10:28-run-at-9:28 scored an overall B because HR and load together
carried 40% and both landed A.

| Intent | distance | pace | hr | continuity |
|---|---|---|---|---|
| easy | 0.19 | **0.42** | 0.24 | 0.15 |
| quality | 0.19 | **0.42** | 0.24 | 0.15 |
| long | **0.45** | 0.20 | 0.20 | 0.15 |
| steady | 0.30 | 0.30 | 0.25 | 0.15 |

An `n/a` metric drops out and its weight redistributes proportionally. Zero
gradeable metrics yields `null` — never `1.00`, because "failed" and "not
measured" must not be the same number.

The mean consumes UNQUANTIZED scores; quantizing first would compound four
rounding errors into the number the reader looks at first. Note this removes a
double quantization the letter path had (`d` → letter → base letter → points →
GPA → cut → letter), and with it `base_letter`'s old contract that a modifier
could never move the overall — sub-band position now propagates, which is where
most of the new resolution comes from.

**The headroom cap.** The overall may never sit more than
`OVERALL_STAR_HEADROOM` (**2.0**) above its worst rated row, reported as
`capped: true` with `capped_by: {metric, stars}` and the uncapped `mean_stars`
kept alongside. A card printing a fine overall above a row that failed is not
reporting a score, it is averaging away the finding — the same rule the old
F-cap enforced, now an invariant of the arithmetic rather than a threshold that
has to fire. `STAR_FLOOR + 2.0 = 3.0` is exactly the C the letter version pinned
to, so it reproduces that rule at the old boundary and degrades linearly on
either side instead of stepping. Measured over 240 cards it fires on 12% and
catches every card the F-cap caught, plus 16 more whose worst row was merely bad
rather than floored — cases the letter cap ignored entirely.

## Returns

Inline/remote PDF responses carry readable Markdown in `content`, an optional
MCP image block, and machine fields in `structuredContent`. Display the content
in the reply; do not reconstruct grades from those fields. `coaching_source`
labels cached/generated/deterministic/fallback prose; `data_quality` carries
coverage dates, refresh guidance, and missing-split notices.

The following JSON shape applies to local table/PDF responses:

A single text content block holding this object. `markdown` is the payload;
everything else is metadata for the caller's own logic.

```json
{
  "markdown": "# Report Card — Interval Session\n**2026-07-21** · 5.95 mi in 1:03:41 · 10:42/mi\n\n## Overall: ★★☆☆☆ 1.85 / 5\n…",
  "activity_id": 23685126977,
  "date": "2026-07-21",
  "overall": {"stars": 1.85, "mean_stars": 1.85, "graded_metrics": 3,
              "capped": false, "capped_by": null},
  "stars": {"distance": 1.31, "pace": 1.0, "hr": 3.76, "continuity": null,
            "load": null},
  "ratings": {"distance": "★¼☆☆☆ 1.31", "pace": "★☆☆☆☆ 1.00",
              "hr": "★★★¾☆ 3.76", "continuity": "n/a", "load": "n/a"},
  "overall_rating": "★★☆☆☆ 1.85",
  "scale_note": "5 stars = the day was executed as prescribed. A compliance score, not a verdict on how good the run was.",
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
| `overall` | `{stars, mean_stars, graded_metrics, capped, capped_by}`. `stars` is the capped value the card shows; `mean_stars` is the uncapped weighted mean, kept so the two can be reconciled. `capped_by` is `{metric, stars}` naming the row that pulled it down. Both `stars` and `mean_stars` are `null` when nothing was rated. |
| `stars` | Per-metric score in [1.00, 5.00], or `null` for an unrated metric. `load` is always `null` — it is a stimulus metric. |
| `ratings` / `overall_rating` | The rendered strings. Prefer these over re-deriving a star row of your own, so every surface shows one rating. |
| `scale_note` | What a 5 means. Carry it when summarising a card aloud. |
| `reference` | The reference *mode* — `plan` never appears here; this is `rolling_60d` or `insufficient_data`, since the mode belongs to the rolling pool. Whether the plan supplied targets is visible in `markdown`'s reference line. |
| `intent` / `intent_source` | e.g. `"interval"` / `"plan"`, or `"easy"` / `"inferred"`. |
| `splits_available` | False for the ~88% of history that was backfilled. |
| `other_activities_on_date` | Present only on a double day, so the other session isn't silently hidden. Each entry is `{activity_id, activity_type, distance_mi, start_time}` — enough to say *which* session went ungraded, which a bare id could not. |
| `path` | Local PDF exports only. |
| `download_url` / `download_expires_in_seconds` | Remote PDF exports only; up to 600 seconds. |

Errors: `no matching activity found` (with the `activity_id` / `date` echoed),
`malformed date '...'`, `unknown format '...'`, `PDF render failed: ...`.

### The markdown card (abridged, real numbers from 2026-07-21)

```markdown
# Report Card — Interval Session
**2026-07-21** · 5.95 mi in 1:03:41 · 10:42/mi

## Overall: ★★☆☆☆ 1.85 / 5
_5 stars = you did what the day prescribed — a compliance score, not a verdict
on how good the run was._

**Distance** — You went 5.95 against a prescribed 5. …
**Pace** — Your best mile was 9:25 against a 6:58 rep target. …
**Heart Rate** — 136 against a 145 floor for a quality day. …
**Stimulus** — 81 banked against an interval-day expectation of 105. …

| Metric | Actual | Expected | Delta | Rating |
| --- | --- | --- | --- | --- |
| Distance | 5.95 mi | 5.00 mi | 0.95 mi long | ★¼☆☆☆ 1.31 |
| Pace | 9:25/mi best mile | 6:58/mi | 147s/mi slower | ★☆☆☆☆ 1.00 |
| Avg HR | 136 bpm | ≥ 145 bpm | 9 bpm under | ★★★¾☆ 3.76 |
| Continuity | — | — | — | n/a |

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

The four labelled paragraphs under the rating line are the coach's read
(`agent/workout_coach.py`). They are told the verdicts are not theirs to revise,
and are forbidden from naming the rating — it prints in the table immediately
below. The prompt is handed a severity WORD in place of every score
(`STAR_VERDICT_CUTS`), so the value it may not repeat is never in its context to
echo. `find_grade_leak` polices both a star rating and a letter grade (an
invented scale is banned just as squarely).

## Example

**Ask:** "how did this morning's run go?"

```
workout_report_card()
```

→ grades the most recent activity and returns report text plus the available
HR image directly in chat. Display both, including data notices. No PDF is
written or opened by default. Use `workout_report_card(format="pdf")` only for
an explicit export request; `format="table"` retains the JSON/Markdown response.

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
- **PDFs use the existing one-page density ladder.** If even the densest
  layout overflows, the response reports `pages`; it never silently hides it.
- **`format="both"` and `format="pdf"` return the same payload**, including
  report text. Default inline returns text plus an HR image without PDF cost.
- **Only explicit local PDF exports may fetch Garmin's detailed HR trace.**
  Inline reports and HTTP exports read the local sample cache, then fall back
  to lap averages. Missing HR yields a report with a clear chart notice.
- **Generated coaching is opt-in for inline, and local-only.** Local table/PDF
  exports preserve generation-on-miss for existing warming scripts. A failed
  generation falls back to computed text and cannot overwrite a stored real read.
- **WeasyPrint needs native Pango/HarfBuzz.** On macOS: `brew install pango`
  then `export DYLD_LIBRARY_PATH="$(brew --prefix)/lib"` (or put it in `.env`).
  `format="table"` needs none of this.
- **Local PDF output location and theming.** Default is a per-process ephemeral
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
- [`generate_brief_report`](generate_brief_report.md) — the daily-brief PDF
  exporter, the only member of `LOCAL_ONLY_TOOLS`.
