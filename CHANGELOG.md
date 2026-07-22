# Changelog

All notable changes to local-fitness are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.28.0] - 2026-07-22

### Fixed
- **A bike ride counted as running distance.** 0.27.0's pace gate answered
  "run or walk" but was asked "on foot or not": a 30km ride paces at about
  2:00/mi, so `_ran` returned True and `_running_distance` counted all 30km as
  run mileage. `_ran` now checks `_is_on_foot` FIRST — the label is unreliable
  about run-vs-walk but perfectly reliable about foot-vs-wheel, since nothing
  logs a bike ride as `treadmill_running`. Caught while investigating the perf
  gate, not by the tests; it has one now.
- **`build_plan_detail` walked each day's activities four times.** Splitting
  foot/run/walk across three helper calls on top of `_workout_actuals` cost a
  **15.4% regression** on `get_training_plan_progress` against CI's 15% gate.
  `_workout_actuals` now returns `(foot, run, walk, pace, types)` from ONE
  pass, evaluating `_ran` at most once per activity.
- **The brief's "fitness sliding" mandate overrode the coach profile on every
  voice.** The steps mandate was correctly gated on
  `profile.includes_harsh_block`, but the conditioning mandate hardcoded
  *"Override the soft coach voice. Be harsh."* unconditionally — so selecting
  `supportive` or `neutral` still produced a roast the moment fitness slid. A
  profile that the prompt carrying it can override is not a profile. Now gated,
  with a profile-deferring twin that keeps every FACT (the CTL slide, the
  training gap, one concrete session) and drops the override.
  **Only the two soft profiles' prompts change; `adaptive` and `hardass` render
  byte-identically**, so the live brief is untouched.
- **`plan_coach` and `workout_coach` hardcoded the user's name into their
  prompts.** "You are Nate's running coach" shipped in tracked code, so a
  stranger's clone was told it was Nate's coach and a changed `user_name`
  setting was ignored on both PDF surfaces. Both now take `user_name`,
  threaded from `config.user_name()` by `tools.py`.
- **`brief_planner` resolved `user_name` with a different default** ("Nate")
  than every other caller (`"the user"`). Same setting, two behaviors.

### Changed
- **One voice definition, composed everywhere.** New
  `prompts.coach_voice_block(user_name, profile, *, compact=False)` and
  `prompts.user_notes_block(user_name, notes_text)` replace four inlined
  copies. Both are pure — `notes_text` is a parameter, never read inside —
  because `plan_coach`/`workout_coach` key their disk caches on a hash of the
  assembled prompt. The two PDF coach prompts consequently gain what they were
  missing: the profile heading, the profile name, and the rule that saved notes
  REFINE (outrank) the profile point-by-point.
- **New `config.user_name()`** — the single resolver, DB > env
  (`LOCAL_FITNESS_USER_NAME`) > `config.DEFAULT_USER_NAME` (`"the user"`),
  mirroring `config.coach_profile`. Documented in `.env.example`.

### Internal
- `tests/test_prompts.py` gains the enumerating gate: every voice-bearing
  surface × every profile must carry that profile's persona, dials, name, the
  configured user name, and the notes-precedence rule — plus an `ast`-based
  guard that fails the build if a prompt module puts a personal name in a
  non-docstring string literal. Adding a prompt surface means adding it to
  `_voice_surfaces`.
- Report-card prompt A/B (5 generations per arm, activity 23685126977):
  `parse_read` 0/5 failures both arms, median latency 9.3s → 9.2s,
  over-budget paragraphs 7/20 → 4/20. The ~10% letter-grade leak rate that
  `_GRADE_TONE` forbids is present in BOTH arms and predates this change;
  filed as a follow-up rather than fixed here.

## [0.27.0] - 2026-07-22

### Changed
- **Both generated PDFs are single-page documents now, by contract.** Measured
  before the change: the 2026-07-22 brief rendered **2 pages** while ~150pt of
  page 1 sat empty, and the report card for a 6-split activity did the same
  (the overflow CLAUDE.md previously documented as accepted). Neither renderer
  had any idea how tall its own output was. `visuals.fit_one_page` now lays the
  document out, counts `len(document.pages)`, and steps down a three-rung
  density ladder (roomy → compact → dense) until it fits; `chart_h_pt` is the
  load-bearing knob, because charts are measurably what broke the page (the
  same brief fit on one page with its charts removed). Shared by both
  renderers rather than reimplemented per renderer.
- **`generate_brief_report` shrinks first, then truncates.** When even the
  densest rung overflows, the lowest-priority takeaways are dropped and the
  page **states how many** ("2 further signals omitted for space") — never a
  silent spill, never a silent hide. Retries after the first attempt use the
  densest preset only, bounding the worst case at
  `len(PRESETS) + len(takeaways) - 1` layout passes (~65ms each).
- **Signal cards now continue below the Training Plan instead of leaving the
  right rail empty.** `cards_in_left_rail` balances the rails, counting the
  plan block as ~2 cards (measured: ~347pt against ~212pt for a card). This
  roughly doubles usable signal area, which is what lets the ladder stop at a
  roomier rung and keep the charts legible.
- **Charts are a wide band (`CHART_FIGSIZE` 8.0×2.8), not 16:9**, with a pinned
  tick count and horizontal x labels. Three cards in a rail now read as one
  system instead of three charts that happen to share a palette.

### Fixed
- **Chart windows were open-ended.** `_fetch_metric_series` anchored to
  `date.today()` with **no upper bound**, so re-rendering a past brief drew
  charts running to today and could show data the brief's own prose never saw.
  It now takes `end` and `generate_brief_report` passes the brief's date —
  the rule `_build_plan_section` already followed. Live callers (`chart`,
  `generate_chart`) keep today's behavior via the default.
- **The plan rollup counted walking as run mileage.** `plans._is_running` is a
  substring match on Garmin's label, and walking-desk sessions log as
  `treadmill_running` — so 07-21 reported `Planned 5.0 mi / Actual 9.2 mi` for
  an interval day whose run was 5.95 mi and whose remaining 3.23 mi was a
  29:15/mi walk. Distance and duration are now gated on **measured pace**
  (`GradingConfig.pace_gated_locomotion`, on by default) via the shared
  `interpret.is_running_effort`. This matters most for duration: that walk ran
  1:34:30, long enough on its own to satisfy any rep-session target. Easy days
  still count walking as active recovery, so prescribed walk days remain
  gradeable. The strip now reads run miles with the walk total named beside it
  (`20.0 mi / 29.5 mi · +9.3 walked`), and the two reconcile by construction.
  **Adherence changes as a result** — the 2026-07-22 brief moved 94% → 88%.
- **The Training Plan rail was vertically centred, not top-aligned.**
  `table.page-layout > tr > td` never matched: HTML parsing inserts an implicit
  `<tbody>`, so the child combinator silently failed and the cells kept
  `vertical-align: middle`, floating the plan in the page's vertical centre
  under a large void.
- **Two measured table collisions.** The week table's Type and Planned columns
  had no gap at all (`interval` and `5.0` extracted from the PDF as the single
  word `interval5.0`); column widths are now budgeted from the longest string
  each column can hold. The stat strip's compound "This Week" value overran its
  half-rail tile and read as one number with the Slips tile beside it; the
  strip is now three narrow tiles over one full-width tile.
- **The Today callout printed the same instruction three times.**
  `plan_coach.fallback_coaching_line` restated both the prescription and the
  description that the callout already prints directly above it.

### Internal
- `is_running_effort` / `RUN_PACE_CEILING_SEC_PER_MI` moved from
  `agent/report_card.py` into `agent/interpret.py` (the pure classifier module,
  per its stated rule) so `plans.py` can share them without a
  `plans → report_card → plans` import cycle. `report_card` re-exports both.
- `plans.load_activities_by_date` now selects `avg_pace_sec_per_km`. It is
  **required, not incidental**: without it the pace gate silently falls back to
  the label, which is exactly the bug it exists to fix — the gate shipped as a
  no-op until this was caught on a live render.

## [0.26.0] - 2026-07-21

### Fixed
- **The report card's reference pool was measuring runs against walks.**
  `activity_type` is Garmin's label, not a measurement: walking-desk sessions
  log as `treadmill_running`, and both the exact-type filter and
  `plans._is_running` (a substring match) passed every one of them through. On
  live data the 60-day pool held 46 activities split 16 real runs
  (8:40–11:46/mi, HR 114–172) against 30 walking-pad sessions (14:08–84:20/mi,
  HR 76–120), so the "median comparable activity" was a 15:50/mi walk at
  116 bpm and 22 training load. A genuine interval session was scoring **A+ on
  both heart rate and training load** — 40% of the composite — for clearing a
  walking bar. Comparability is now gated on measured locomotion
  (`RUN_PACE_CEILING_SEC_PER_MI`, a 13:00 mile) *before* the type filters, so
  widening can't drag the walking corpus into a thin running pool. A paceless
  row has an unknown mode and joins neither side. The card states the exclusion
  count, since the filter is invisible in the numbers otherwise.
- **Interval pace was a structurally guaranteed F.** A plan's quality-day pace
  describes the *reps*, but `avg_pace_sec_per_km` averages in the warmup, the
  recovery jogs and the cooldown — so grading one against the other returned F
  for every correctly-executed interval session. Measured: a 6:58/mi
  prescription averaged 10:42/mi and scored F while its 4th mile ran 9:25 at
  164 bpm. Quality-day pace is now graded on the fastest full split — the one
  documented exception to "no grade reads `activity_splits`" — and returns n/a
  *with a stated reason* when there are no splits, rather than falling back to
  the comparison it exists to avoid.
- **A training-load spike scored A+ directly above its own spike warning.**
  `load_deviation` was one-sided-low and uncapped, so a day at 81 load against
  a 22 expectation printed A+ on the row above "**spike** — more than double
  your median day", while the coaching read called it "stacking debt". Load is
  now penalized in both directions past `LOAD_SPIKE_FACTOR`; landing at or
  under the threshold is still a clean A.
- **The composite outvoted the point of the workout.** `overall_grade` is now
  intent-weighted (`INTENT_METRIC_WEIGHTS`): pace carries an easy or quality
  day, distance carries a long run. Flat weights let HR and load (40%
  combined) score a prescribed 10:28 easy run executed at 9:28 an overall B on
  a card whose own read called it "a race finish". An F on any metric also caps
  the overall at C, stated in the notes.
- Ungraded metrics no longer print a Delta — a "224s/mi slower" beside an n/a
  re-made the very comparison the n/a refuses.

### Changed
- **`workout_coach` no longer follows `briefing.DEFAULT_MODEL`.** That constant
  also drives the daily brief, where a model change is a prompt change that has
  to clear the scorer and a cross-model A/B first — so the coupling meant the
  report card's read could not be tuned at all. It now carries its own
  `DEFAULT_MODEL`, sized to what the call actually does: phrase four 45-word
  paragraphs from grades `report_card.py` already computed. `effort` and
  `thinking` are set explicitly and are load-bearing rather than polish —
  current Sonnet runs adaptive thinking whenever `thinking` is unset, so moving
  the model ID forward without them would have made an already-67s call slower.
- `HR_BANDS` re-verified against the cleaned distribution and deliberately left
  unchanged: excluding walking-pad sessions moves the `treadmill_running`
  median from 116 to 145, which now agrees with the never-contaminated outdoor
  median of 144.5, so the constants describe both pools.

### Documentation
- **New: [`docs/mcp/`](docs/mcp/) — a reference page for every MCP tool.** One
  file per tool (37), each with parameters, the real return shape, a worked
  example, and the gotchas that only show up in the handler. Plus an index that
  groups them by area, states the stdio-vs-HTTP availability split and the rule
  behind it, and carries the training-plan state machine as a diagram.
- **README corrected.** It advertised **33 tools**; the real surface is 37 over
  stdio and 35 over HTTP. Six tools were missing from its list entirely
  (`plan_chart`, `generate_chart`, `sync_garmin_data`, `get_brief_context`,
  `workout_report_card`, `generate_brief_report`), `activity_hr_samples` was
  missing from the table list, and the project layout predated `plans.py`,
  `interpret.py`, `report_card.py`, `workout_coach.py`, `visuals.py` and
  `branding.py`. The tool list now summarizes by area and links to `docs/mcp/`
  rather than duplicating it — duplication is why it went stale.
- The audit surfaced defects beyond documentation; filed as #131 (`run_sql`
  silently truncates at 500 rows and reports a post-truncation `count`), #132
  (`update_user_note`/`delete_user_note` address notes by unstable line index
  and can silently hit the wrong one), and #133 (a dozen smaller
  description-vs-handler mismatches).

### Fixed (documentation-adjacent)
- `chart`'s `style` schema described `line` as a "colored value-line (heat emoji
  on an invisible canvas), weekly-averaged past ~5 weeks". It is none of those:
  `charts.render_line` draws a **monochrome box-drawing curve**, and only `bar`
  and `combo` are weekly-averaged (past 21 days, not ~5 weeks). The model was
  being told the wrong thing about a tool it picks a style for.
- `visuals.py`'s module docstring still filed `generate_chart` under
  `LOCAL_ONLY_TOOLS`; it moved into `ALL_TOOLS` on 2026-07-13.
- CLAUDE.md's plan-lifecycle bullet was stale on `update_plan_workout`: it
  omitted `duration_min`, and implied the tool can move or add a day. `date` is
  the `UPDATE`'s key rather than an editable column, and a day with no existing
  prescription errors rather than inserting — so "move Saturday's long run to
  Sunday" is two calls, not one.

## [0.25.0] - 2026-07-20

### Added
- **`workout_report_card` — a graded report card for one workout.** The coach
  could already *describe* a workout (`get_workout_detail`) but nothing
  *judged* one, so the same run got called "solid" one day and "flat" the
  next. Grading is now tested Python (`agent/report_card.py`), per the
  `interpret.py` rule that the LLM phrases a judgment but never derives one
  code can compute. Four metrics — distance, pace, HR, training load — each
  reduce to a single non-negative relative deviation passed through ONE
  shared band table, so the rubric stays testable. Returns a `markdown` card
  plus a PRESS-themed PDF with a per-mile split table and an HR bar chart.
  - **Two reference points, always named on the card**: the active plan's
    prescribed workout for that date (distance + pace only — `plan_workouts`
    has no HR or load column), else a 60-day rolling *median* of comparable
    activities. Below 5 comparable activities the card returns n/a and says
    why rather than grading against noise.
  - **Direction gating** keeps the rubric honest: an easy run is *supposed*
    to be slow, so easy/long days are penalized only for running too fast and
    quality days only for too slow, with each metric's expectation scaled by
    the workout's intent.
  - **Comparability is exact `activity_type` first**, widening to the on-foot
    class only when the pool is too thin. Measured on live data: pooling
    `running` with `treadmill_running` put median HR at 119 against an
    outdoor average of 140 and handed a normal easy run a D — an artifact of
    mixing two HR regimes, not a judgment.
  - **Splits are presentation-only.** No grade reads `activity_splits`: only
    87 of 747 activities have them (written by daily sync, never by
    backfill), so a splits-dependent grade would be silently unavailable on
    ~88% of history.
  - **Local-only**, alongside `generate_brief_report` and for the same
    reason: it hands back a filesystem path, which is meaningless to a
    remote `/mcp/` caller.
- **The card opens with the coach's read — four short paragraphs, one per
  graded area.** A new `agent/workout_coach.py` (sibling of `plan_coach.py`)
  phrases what the already-computed grades MEAN, in the resolved coach voice
  and honoring saved user notes. Toolless single-shot SDK call behind a
  single-entry disk cache, with a deterministic four-section template fallback
  — a missing credential or a dead stream costs the phrasing, never the card.
  - **It never names a letter grade.** The letters print in the table directly
    below; repeating them spends the only words the read gets. It makes the
    reason obvious from the numbers instead.
  - **Hindsight and foresight.** The read is given the graded day's trailing
    runs and the next 7 days of prescriptions, so it can say "two days after a
    412-load session" and "intervals hit Tuesday" rather than judging the run
    in isolation.
  - Output is parsed into typed sections (`parse_read`); a generation missing
    any of the four falls back to the template rather than rendering a card
    with a blank paragraph, and is never cached.
  - The training-load model (CTL/ATL/TSB) is deliberately withheld from this
    prompt — it is printed elsewhere on the card, and handing it over bought a
    freshness lecture in place of a distance verdict.
- **Per-tenth-of-a-mile HR chart, fully labeled.** A new
  `ingest/details.py` fetches one activity's ~1700-sample HR trace on demand
  (never as a backfill — 747 activities of detail calls is the shape that
  trips Garmin's 429) and caches it in a new `activity_hr_samples` table.
  `report_card.bin_hr_trace` averages samples into tenth-mile buckets; the
  chart carries an axis title, both axis labels, and the run's own average as
  a reference line. Falls back to the per-lap chart whenever no trace is
  available, so an offline render or an activity Garmin has no details for
  behaves exactly as before.
- **Pace is overlaid on that chart as a line on its own right-hand axis.**
  Per tenth of a mile, measured as elapsed time over ground covered (not an
  average of instantaneous speeds, which would weight a stopped second like a
  moving one). The axis is **inverted so faster is up** — pace is
  seconds-per-mile, so on a natural axis a surge would dive while the HR bars
  it caused rose beside it, and the two series would read as disagreeing — and
  ticks render as `m:ss`, since minutes per mile is base 60. The time channel
  is `sumDuration`, the one the activity summary's average pace comes from, so
  the chart can't contradict the pace printed in the table above it. Bins with
  no usable time are gaps in the line rather than invented paces; the HR bars
  render regardless.
- **Per-mile table drops its Distance column** as duplicative — the row label
  already is the distance, so it printed "1.00 mi" beside a column headed
  "Mile".
- **The Grade column is left-aligned** like every other column in both tables.
- **The standalone "graded against…" sentence is gone.** The Expected column
  states each metric's target, which is where a reader checks it; the
  plan-vs-median disclosure now rides the hero's meta line as
  `easy (plan)` / `easy (60d median)`.

### Fixed
- **The coaching-read cache key now includes `activity_id`.** Two sessions on
  the same day with the same name and the same grades — a double day, which
  the tool already reports via `other_activities_on_date` — hashed identically,
  so the second card served the first's read.
- **Heart rate was graded against an unreachable band.** The easy ceiling of
  0.88x the rolling median asked for a number that appeared in 1 of 13 real
  runs in the window — the reference median is taken over all comparable
  activities, which for a mostly-easy runner is itself near easy HR, so
  demanding 12% below it made the HR grade a standing penalty rather than a
  judgment. Recalibrated (easy 0.97, long 1.00, quality 1.00, steady
  0.93-1.07).
- **The HR row contradicted itself.** It displayed the bare median as
  "Expected" while grading against the band edge, so an easy run at 136 against
  a 146 median rendered as "-7%" beside a B+ when the real finding was 6% ABOVE
  the ceiling that produced the grade. Expected is now the bound the grade was
  actually measured against, shown as the band ("<= 142 bpm"), and a run inside
  the band reads "in range" rather than a percentage against one edge.
- **A missed prescription could still earn an overall A.** Plan targets and
  rolling medians were held to identical tolerance, so a prescribed 10:28 easy
  run executed at 9:28 — a full minute per mile fast, the entire failure mode
  an easy day has — scored a B- and the card printed an overall A for a run its
  own coaching read called "you never ran easy at all". Plan-referenced
  distance and pace are now graded on tightened bands (`PLAN_TIGHTEN`), because
  a plan is an explicit instruction and a median is a fuzzy reference.
- **Tests no longer make live Garmin calls either.** The PDF path resolves an
  HR trace, so a test that merely rendered a PDF hit the activity-details
  endpoint for a fixture id — visible only as a 404 in the logs of a passing
  test. The conftest guard now covers Garmin alongside the SDK.
- **The report card's SDK timeout was too tight.** Measured at 22.2s against a
  30s ceiling, so an ordinary cold start silently served the template fallback.
  Now 90s.
- **Tests no longer make live Claude calls.** Every report-card render
  generates a read, so the suite quietly started making real network calls —
  10 seconds became 7 minutes, at real cost, while still passing. A conftest
  autouse fixture now blocks `claude_agent_sdk.query` outright; callers
  degrade to their deterministic fallbacks, and a test wanting specific
  generated text patches its own generator.

## [0.24.1] - 2026-07-19

### Fixed
- **PRESS report content no longer paints past the printable area.** The
  page-layout cells' width percentages plus their em paddings summed past
  100%, pushing the plan rail's mono week table up to 14pt beyond the
  @page margin (and the coaching line 4pt). Cell widths now account for
  the padding (54+42+~4%), the week table is `table-layout: fixed` with
  explicit column widths and MM-DD dates (year dropped — noise in a
  7-day window), and a bounding-box regression test measures every
  rendered word against the printable edge so overflow can't silently
  return.

## [0.24.0] - 2026-07-19

### Added
- **Brand-themed PDF/PNG rendering, local-overridable.** Every generated
  PDF (brief report) and chart PNG is styled from a brand theme
  (`agent/branding.py`). The checked-in default is **PRESS** — the repo
  owner's editorial brand: warm cream paper, near-black ink rules, one
  orange signature accent, sans 800–900 structure + serif-italic
  commentary + mono data voices, no rounded corners/shadows/gradients.
  The report gets a masthead (heavy ink rule, rotated accent stamp,
  tracked-caps eyebrow, byline), ruled editorial signal sections with
  serif standfirsts, PRESS numerals in the plan rail, and typographic
  PRESS-strict tones/verdicts (done = ink, partial = dim italic,
  MISSED = the one accent). Charts render on paper with ink marks and an
  accent trend line. Set `LOCAL_FITNESS_BRAND_FILE` to a JSON file to
  deep-merge your own colors/fonts/identity over the default (see
  `.env.example`); `fonts.mono_file` embeds a real TTF via @font-face.
  A missing/broken brand file never breaks a render.

## [0.23.0] - 2026-07-19

One consolidated release for the day's facet-review loop: four review
passes over three weeks of real interaction transcripts and launchd logs
(accuracy / completeness / efficiency / agent-UX, then focused charts /
plan-tools / prompt-drift deep-dives), every confirmed finding implemented.

### Fixed
- **Nightly brief no longer dies on one bad SDK stream.** The Agent SDK
  stream was observed dying two ways (idle-out with zero output after
  3–5 minutes; subprocess crash mid-stream) and a single failure cost the
  whole day's brief — 5 of the 8 mornings before this release had no
  brief. `generate_and_save` now makes up to 3 attempts, and a
  per-message idle watchdog (120s) kills a hung stream fast instead of
  letting it burn minutes producing nothing. Env knobs:
  `LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S` / `_BRIEF_MAX_ATTEMPTS` /
  `_BRIEF_RETRY_DELAY_S`.
- **Empty generator output now reports the real cause** ("stream died"
  diagnostic) instead of falling into the JSON parser as the misleading
  "no JSON found in agent response".
- **Grounding invention-rate signal un-saturated.** Matching is now
  kind-partitioned (percent tokens vs plain magnitudes), killing the
  cross-unit false positives (HR cap 140 bpm "matching" 147% of step
  goal) that pinned the brief's only automated accuracy monitor at 1.000.
- **V1 brief rollback regained plan-awareness.** The brief loop's frozen
  read-only allow-list never granted `get_training_plan_status` despite
  the V1 prompt instructing "call it FIRST" — plan-aware briefs were
  silently dead on the rollback path since the 2026-06-27 V2 cutover.
- **`type='rest'` re-prescription no longer leaves the old hard-run
  description** on the rest day (defaults to "Rest day").
- **`revise_training_plan(goal_type=...)` re-derives `goal_distance_m`**
  — a 10k→half revision previously kept 10000 m, so the Riegel
  projection predicted a 10k finish labeled as a half.
- **PNG charts autoscale the value axis to the data band — never
  zero-anchored.** A 48–57 bpm resting-HR band rendered as a flat sliver
  atop a 0–57 axis (line `fill_between` filled to y=0; bars anchored at
  0). New pure `value_axis_bounds` helper applied to all three PNG chart
  types (covers `generate_chart` and the PDF's embedded takeaway charts).
- Flat non-zero ASCII bar charts paint neutral mid-heat instead of
  "coldest" blue; short-window line charts no longer overflow the
  footer; stale retired-UI comments/docstrings corrected.

### Added
- **Brief failure is now a signal, not silence.** `fitness brief` fires a
  distinct macOS failure notification when generation fails; previously
  the only failure signal was the absence of the success notification.
- **Brief staleness is visible from the tool surface.**
  `assemble_status()` (→ `get_today_status` / `daily_snapshot`) carries
  `latest_brief_date` + `brief_stale_days`; the `fitness://brief/latest`
  resource and the `/coach` prompt snapshot both flag a stale brief.
- **launchd 09:30 backstop fire + `fitness brief --if-missing`** — the
  plist template fires at 06:30 and 09:30 with the same command: a no-op
  when today's brief exists, a full retry when the 06:30 run failed.
  Re-run `ops/install-launchd.sh` to apply.
- **`plan_chart` MCP tool + `render_plan_vs_actual` renderer** — the
  scheduled-vs-actual training view (█ = miles run, ░ = shortfall vs
  plan, verdict glyph per row), daily rows or Monday-anchored weekly
  buckets for long windows.
- **Long-window `bar`/`combo` charts weekly-bucket instead of
  degrading** — a "90-day bar graph" ask is now honorable.
- **`update_plan_workout` gained `duration_min`** (the graded field for
  tempo/interval sessions previously had no tool path) **and `seq`**
  (double-day AM/PM sessions were schema-legal but only the morning row
  was editable); progress payloads carry `seq`.
- **Structural (plan_id, date, seq) uniqueness** via a partial unique
  index backing the validation-time dedup.
- **PDF coaching line cached per input-set** —
  `plan_coach.generate_coaching_line_cached` keys a single-entry disk
  cache on the pure `build_prompt` hash (9 identical same-day SDK calls
  observed from repeat PDF renders); failures are never cached.

### Changed
- **Prompt-text drift corrections** (verified via `score_prompt` 11/11
  before and after + a rendered-prompt differential diff): the chat/coach
  system prompt no longer claims blanket "read-only access" while
  instructing note/plan writes (Garmin metrics are read-only; writes go
  through the dedicated tools); both brief prompts drop the retired-UI
  "so the UI can render" phrasing.
- CLAUDE.md's brief failure-signature documentation rewritten to
  distinguish credential-missing (fails before first message) from SDK
  stream death (ttfm present, then empty/partial) — the old note blamed
  the credential for what was stream instability.

## [0.22.2] - 2026-07-13

### Fixed
- **Brief takeaway tables could render as one flattened line of pipes.**
  Two gaps in the deterministic table-repair layer
  (`render.fix_table_row_breaks`), both observed live in the 2026-07-13
  brief: (1) a table glued directly to surrounding prose
  (`**Header:**\n| Metric |…`) is not parsed as a table by strict
  CommonMark/GFM renderers — repair now isolates table blocks with blank
  lines on both sides; (2) the dropped-backslash `\n` artifact could land
  after the final row's closing pipe (`| ↓ |n`), which the `|n|`-only
  repair missed — now stripped. Both idempotent, prose untouched,
  regression-tested against the observed shapes.

## [0.22.1] - 2026-07-13

### Fixed
- **`run_sql`'s `fitness://schema` error pointer never fired on real
  invalid queries.** A mistyped table/column raises
  `sqlite3.OperationalError`, which the 0.22.0 rewording left on the old
  generic "operational error" branch — the schema-resource remedy lived
  only in the practically-unreachable `sqlite3.Error` branch. Caught by
  the post-merge live MCP quality gate; both branches now carry the
  pointer, with a regression test on the real bad-table path (the
  time-budget message is unchanged).

## [0.22.0] - 2026-07-13

### Fixed
- **The brief PDF's 2-column signal-card grid never actually rendered as
  2 columns.** Shipped in the 0.20.0 redesign, the `display: flex;
  flex-wrap: wrap` (and a subsequently-tried `display: grid;
  grid-template-columns: 1fr 1fr`) CSS both looked correct as source but
  WeasyPrint 69.0 rendered every card on its own row regardless — a gap
  only caught by objectively measuring rendered word positions with
  `pdfplumber`, not by eyeballing a thumbnail or checking HTML class
  names. Replaced with an HTML `<table>` (the one layout primitive
  WeasyPrint reliably supports for this), verified correct against real,
  wrapping takeaway content, with new geometry-based regression tests
  (`tests/test_visuals.py`) that assert actual rendered x/y positions
  instead of just HTML structure.

### Changed
- **The brief PDF is redesigned to fit one page and look more polished.**
  The whole-page layout is now 2 columns — the signal-card rail (left)
  runs in parallel with the Training Plan section (right) instead of
  stacking sequentially — which buys back the vertical room a 3-5
  takeaway brief plus a full plan section needs to fit on one A4 page.
  Built as an HTML `<table>` (the one layout primitive WeasyPrint 69.0
  reliably supports for real multi-column placement — flex/grid were
  both tried and both silently collapse to one column for this content
  shape). Signal cards get a tone-tinted background wash and rounded
  left-accent border instead of a plain ruled list; the header gains a
  colored rule and a pill-styled date badge; the plan rail's stat strip
  becomes a 2x2 tile grid (the narrower rail can't fit 4 tiles across).
  Verified 1-page fit via `pdfplumber` page-count checks against the
  documented 3-5 takeaway maximum, not by eyeballing a render.
- **Interpretation parity on the ad-hoc analysis tools.** The brief path has
  always computed judgments (TSB zones, trend arrows, effect reads) in tested
  Python and left the model to only phrase them; the ad-hoc MCP tools
  (`training_load_status`, `correlate`, `find_anomalies`, `compare_periods`,
  `get_metric_trend`) left the same judgments to the connecting LLM, applying
  static legend text by hand. New pure module `agent/interpret.py` supplies
  shared classifiers — `tsb_zone`, `pct_change`, `trend_direction`,
  `delta_direction`, `baseline_position`, `correlation_read`, `effect_size`,
  `sd_position` — and both `status.py` and `brief_planner.py` now delegate to
  it instead of keeping their own copies. Payload adds: `training_load_status`
  gains `tsb_zone`/`ctl_pct_change_14d`/`ctl_direction` (the 14-day CTL "then"
  value is now a shared `brief_planner.ctl_at_or_before` lookup, so the brief
  and the tool agree by construction); `correlate` gains `strength`/`direction`
  and drops its static legend string; `find_anomalies` rows gain
  `sd_distance`/`direction`; `compare_periods` gains `delta_pct`/`cohens_d`/
  `magnitude`; `get_metric_trend` gains `slope_direction`/`vs_baseline`. All
  analysis-tool floats are now rounded at the payload boundary (3 dp for
  correlation/slope/cohens_d, 2 dp for means/SDs/deltas, 1 dp for percentages),
  skipping `None` rather than raising.
- **Plan-tool payload quality.** `get_training_plan_progress` gains a
  `this_week` rollup (`week_planned_mi`/`week_actual_mi`/`slips`, shared with
  the PDF's identical section via a new `tools.weekly_rollup`) and a
  `goal_gap` (`plans.goal_gap`: gap seconds/pct/on-pace vs. the Riegel
  projection). Its `workouts` list is now windowed by default (trailing 14 +
  upcoming 7 days anchored to the data frontier) — pass `full=true` for the
  complete plan across the whole date range. Both plan tools gain formatted
  time fields (`target_duration_formatted`, `target_time_formatted`,
  `predicted_finish_formatted`) alongside the existing raw seconds.
  `get_workout_detail`'s splits and `recovery_pattern`'s matched workouts now
  carry mile/pace fields under the same miles-display gate as the rest of the
  API. `compare_periods` accepts `distance_meters` as a SUM metric (total
  mileage per period, with `delta`/`delta_pct`) for "how much did I run this
  week vs. last."
- **Agent UX surface.** `system_prompt` gains an explicit "Charts" bullet so
  the connecting LLM reproduces `chart` output in the reply instead of
  leaving it collapsed in the tool call. `plan_coach.build_prompt` /
  `generate_coaching_line` now accept the same user-notes and
  metric-translation context as chat/brief, so a saved preference is honored
  in the PDF's coaching line too. Sleep now renders as `"7h 33m"`
  (`units.format_hm`) everywhere it's shown — the status snapshot's sleep row
  as well as the existing brief grounding pool — replacing a raw-seconds
  display in the snapshot table. `training_load_status`,
  `log_manual_workout`, and `delete_manual_workout` error strings no longer
  point MCP-only users at CLI commands they can't run. Tool descriptions for
  `daily_snapshot`/`get_today_status`/`get_brief_context` now state explicitly
  when *not* to use them. `generate_chart` now returns its PNG as an inline
  MCP image content block (in addition to the saved file path) and has moved
  into `ALL_TOOLS`, making it reachable over the networked `/mcp/` transport
  for the first time. `get_today_status` now converges on the same
  `assemble_status()` path as `daily_snapshot`.
- **Layer-separation hardening.** The brief PDF's coaching line — the one LLM
  output entering a user-facing artifact with no numeric validation — is now
  checked by a new `plan_coach.ground_coaching_line`, logged advisorily
  alongside the existing V2 grounding signal, never gating the PDF. The V1
  brief-generation rollback path (`LOCAL_FITNESS_BRIEF_V2=0`) now also
  assembles a grounding pool (for measurement only, wrapped in
  `try/except Exception` so a planner failure can't break the rollback path
  itself) instead of losing the invention-rate signal entirely.

No new user-facing features — this is an efficiency, UX, and layer-separation
release across the MCP tool surface.

## [0.21.0] - 2026-07-09

### Removed
- **The web UI is retired.** The entire `web/` directory (React/Vite SPA,
  ~3,800 LOC), every UI-only REST route (`/api/today`, `/api/metric/{name}`,
  `/api/training-load`, `/api/workouts`, `/api/workout/{id}`, `/api/brief`,
  `/api/sync`, `/api/sync/status`, `/api/plan*`, `/api/activity-heatmap`,
  `/api/strength-volume`, `/api/pace-efficiency`, `/api/status`,
  `/api/config`, `/api/auth/verify`, `/api/notes*`), the
  background-sync-orchestration subsystem, and SPA static-file serving are
  gone. Docker's `web-builder` stage, CI's frontend build/test steps,
  `.github/dependabot.yml`'s npm entry, and `codeql.yml`'s
  javascript-typescript matrix leg are removed alongside it. `fitness
  serve`'s `--open` flag (opened a browser to the now-nonexistent SPA) is
  also gone.

### Added
- **Three new MCP tools cover the plan-lifecycle actions the UI's
  commit/delete buttons used to be the only path for**: `commit_training_plan`
  (activate a draft), `discard_training_plan_draft` (drop a draft without
  activating it), and `abandon_active_plan` (archive the active plan with
  nothing queued to replace it — no undo). The agent now owns the entire
  training-plan lifecycle.

### Changed
- **`_is_public_path` now denies by default.** Only `/health` is explicitly
  whitelisted; every other path (including `/mcp/`) requires the bearer
  token whenever one is configured. The old blanket-public fallthrough for
  non-`/api/` paths existed only to let an unauthenticated browser tab load
  the SPA shell — with no SPA left, deny-by-default is the correct posture
  per this repo's own security convention (whitelist explicitly, don't rely
  on a permissive default).
- **MCP tool speed: the brief/plan read paths now share ONE SQLite
  connection per call instead of opening a fresh connection per lookup.**
  `assemble_brief_context` (brief_planner.py) went from 9 `db.connect()`
  opens to 1 when a training plan is active; `get_training_plan_progress`
  went from 6 to 1; `get_training_plan_status` and `_build_plan_section`
  each went from 4 to 1 (measured on a synthetic multi-year fixture — see
  `tests/test_perf_benchmarks.py`). `db.py`, `config.py`, `plans.py`, and
  `agent/coach.py` gained an optional `conn: sqlite3.Connection | None`
  parameter on every function in these call chains — additive, no
  behavior change for existing callers that omit it.
- **`agent/status.py`'s `_metric_rows` collapsed from up to 4 queries to
  1.** The per-trend-metric `WHERE <metric> IS NOT NULL` query is replaced
  by a single trailing-window `SELECT *` with per-column null-filtering
  done in Python.
- **`agent/brief_planner.py`'s `_compute_signals` no longer scans the
  entire `activities` table.** The run-history query is now bounded to a
  35-day lookback (`_ACTIVITY_LOOKBACK_DAYS`) instead of unbounded back to
  account creation. Accepted tradeoff: `days_since_last_run`/`recent_te`
  read `None`/fewer-than-5 instead of the true (larger) value when a
  runner's last run predates the 35-day window — covered by a regression
  test.
- **New `pytest-benchmark`-based perf-eval harness** (`tests/test_perf_benchmarks.py`,
  `scripts/perf_fixture.py`, `.github/workflows/capture-perf-baseline.yml`),
  proving the above with before/after connection counts and a CI-gated
  latency comparison (`--benchmark-compare-fail=min:15%`) against a
  committed `ubuntu-latest`-captured baseline — not eyeballed. Skipped on
  every ordinary `pytest` run (`--benchmark-skip` in `pyproject.toml`);
  only an explicit `--benchmark-only` invocation runs it.

## [0.20.0] - 2026-07-09

### Changed
- **`generate_brief_report`'s PDF redesigned: signal cards now render in a
  2-column layout, and a new Training Plan section is added below them.**
  The existing takeaway cards reflow into a flexbox grid (robust to any
  takeaway count — an odd-count last card spans the full width rather than
  leaving a gap). The new section shows adherence %, days-to-race, this
  week's planned/actual mileage, a slip count, today's prescribed workout
  with a Claude-generated coaching line (same model as the real daily
  brief, called fresh on every render, with a deterministic fallback if
  the call fails), and a table of the last 7 days graded against
  prescription (done/partial/missed/rest/scheduled). Computed live from
  `plans.py` at render time, keyed to the brief's own date — no changes to
  the `Brief` schema or the daily brief generation pipeline. Whole section
  is omitted when there's no active plan or no plan data for the window.

## [0.19.0] - 2026-07-09

### Changed
- **`generate_brief_report`/`generate_chart` now default to an ephemeral,
  auto-opened output directory instead of persistent `./reports/`.** When
  `LOCAL_FITNESS_REPORTS_DIR` is unset (the common case), output now goes
  to a per-process `tempfile.mkdtemp()` directory — PID-embedded naming,
  cleaned up via `atexit` when the `fitness mcp-stdio` process exits, with
  a liveness-checked stale-directory sweep as a backstop for abrupt
  (`SIGKILL`/`SIGTERM`) exits. After a successful write, the file is now
  auto-opened via macOS `open` (best-effort, never fails the tool call).
  Setting `LOCAL_FITNESS_REPORTS_DIR` still opts back into the old
  persistent-directory behavior (still auto-opened, no auto-cleanup). This
  closes the gap where getting the "pretty" PDF/PNG version of a report or
  chart required a second explicit ask and left files to accumulate
  unopened in `./reports/` forever.

## [0.18.0] - 2026-07-08

### Added
- **Two new local-only MCP tools: `generate_brief_report` and `generate_chart`.**
  `generate_brief_report` renders a saved daily brief into a polished PDF
  (WeasyPrint, reusing the sibling `budget` project's validated color theme);
  `generate_chart` renders a standalone matplotlib PNG for any ad-hoc
  trend/metric question. Both write to local files under
  `LOCAL_FITNESS_REPORTS_DIR` (default `./reports/`, gitignored). Reachable
  ONLY via the stdio MCP transport (`fitness mcp-stdio`) — structurally
  excluded from the authenticated streamable-HTTP `/mcp/` transport via
  `agent/tools.py`'s `LOCAL_ONLY_TOOLS`, since a phone-triggered call over
  that transport would get back a container-internal path with no way to
  retrieve the file. Requires native Pango/HarfBuzz libraries (`apt-get` on
  Linux/CI; on macOS, `brew install pango` plus
  `DYLD_LIBRARY_PATH=$(brew --prefix)/lib` — see `.env.example`).

## [0.17.0] - 2026-07-08

### Changed
- **Alt-model shadow-run path is model-agnostic — any model the `opencode`
  CLI can reach, not just gemma4/Ollama.** The dispatch key changes shape
  from `"ollama:<model>"` to `"opencode:<provider>/<model>"` (e.g.
  `"opencode:opencode/deepseek-v4-flash-free"`), a breaking rename with no
  compatibility shim (internal shadow-run-only diagnostic tool, not a public
  API). Dispatch is capability-aware, not uniform: `provider == "ollama"`
  keeps the existing direct-HTTP, grammar-constrained-decoding path
  (`local_model.py`, unchanged); every other provider routes through a new
  `agent/opencode_model.py` subprocess transport that shells out to
  `opencode run`. gemma4's tuning (tightened schema, temperature, plan-fact
  injection) moves into a per-model profile registry
  (`briefing._MODEL_PROFILES`) so a new model gets zero extra tuning by
  default instead of requiring new `if model == ...` branches. Requires a
  one-time, tool-free opencode agent (`LOCAL_FITNESS_OPENCODE_AGENT`, see
  `.env.example`) — a fail-closed pre-check refuses to send a prompt if that
  agent isn't configured, rather than risking opencode's undocumented
  silent fallback to its tool-enabled default agent. The fixture-only
  safety gate (`_assert_fixture_only_data`) now also checks the user-notes
  path, closing a real gap where un-isolated callers could ship real
  personal notes to a third-party model. Shadow-run-only — never the live
  production 06:30 brief job. See
  docs/plans/2026-07-08-model-agnostic-shadow-run-design.md.

## [0.16.0] - 2026-07-07

### Added
- **New MCP tool: `sync_garmin_data`.** MCP-only clients (opencode, Claude
  Desktop, etc.) previously had read access to the fitness DB but no way to
  refresh it — only the CLI (`fitness pull`) and the web UI's `/api/sync`
  could trigger a live Garmin pull. The new tool wraps the same bite-sized,
  gap-aware `ingest.daily.pull()` + baseline recompute the web UI uses, so an
  MCP-connected agent can sync on request instead of only ever answering from
  stale data. Deliberately excluded from the brief loop's read-only tool
  allow-list — brief generation stays side-effect-free.

## [0.15.2] - 2026-07-01

### Fixed
- **Garmin pulls reuse a cached session token instead of a full login every
  time.** `daily._client()` now passes an explicit tokenstore path to
  `client.login()` (defaulting to `~/.garminconnect/garmin_tokens.json`, the host
  side of the container's `${HOME}/.garminconnect` bind-mount; `GARMINTOKENS`
  overrides it) so a pull resumes the saved session rather than doing a fresh SSO
  login. Repeated logins were tripping Garmin's rate limit
  (`Mobile login returned 429`), leaving the host's 06:30 launchd job re-logging
  in every run; the host now gets token reuse by default (previously only the
  container did, via its `GARMINTOKENS` env var), which also makes the documented
  host-writes-token → container-reads-token seeding flow work for the first time.
  The cached OAuth token eventually expires — re-seed with an interactive
  `uv run fitness pull` when it lapses.

## [0.15.1] - 2026-06-27

### Changed
- **Brief composer cut over to V2 (agent/code separation), default ON.** The
  daily brief now runs a deterministic `brief_planner` (triggers, fixed
  priority, advisory tone → a typed `BriefContext`) → ONE **toolless** generator
  (`max_turns=1`, no MCP) on the shrunk `brief_v2_*` prompt → an advisory
  `grounding.flag` invention-rate *signal* (logged, never a gate). The V1
  tool-driven monolith (`max_turns=20`, MCP tools) is retained as the instant
  rollback: `LOCAL_FITNESS_BRIEF_V2=0` (or `false`/`no`/`off`). Gated on a live
  shadow-run that held structural parity across all six golden fixtures. Only
  the in-process composer is V2 — the `mcp__fitness__*` tools and the MCP chat
  brief still use the V1 tool-driven path (deliberate scope choice).

### Added
- **`get_brief_context` MCP tool** — returns the planner's typed `BriefContext`
  (candidates + snapshot + baselines + training load + 14d workouts + anomalies
  + plan + continuity) in one call, porting reasoning-in-code to the external
  MCP chat path. The MCP `_brief_prompt` now composes from
  `assemble_brief_context()` through the V2 prompt instead of the V1
  `briefing_prompt()`.
- **`grounding.log_grounding(brief, context)`** — a shared public grounding
  function (the advisory invention-rate signal; never gates or mutates the
  brief), with golden eval fixtures, a committed `baseline.json`, and
  `scripts/{capture_baseline,shadow_run}.py` for cost-capped live structural
  parity checks.
- **`update_plan_workout` tool — the agent is now the plan write path.** A new
  MCP tool re-prescribes a single day on the *active* training plan (move a long
  run, swap days, adjust a session): `update_plan_workout(date, type?,
  distance_mi?, pace_min_per_mi?, description?)`, with `type='rest'` clearing
  distance/pace. Backed by `plans.update_active_workout`, which whitelists
  *prescription* columns only — it can re-prescribe a day but can never re-key,
  re-status, or restructure the plan (the single-active invariant and draft
  gating for plan *structure* are untouched). This makes the agent the source of
  truth for plan edits and the web UI view-only (owner's design decision);
  whole-plan changes still go through the draft `propose`/`revise` flow.

### Fixed
- `ab_brief --run` no longer overwrites the live `briefings/<date>.json` (the
  eval `_generate` now defaults `save=False`) and no longer aborts the whole run
  on a single unparseable generation — per-generation failures are recorded as a
  flake *rate* instead of crashing. Threaded `today` through `assemble_status`,
  fixing a latent wall-clock reproducibility bug in the trend window.

## [0.15.0] - 2026-06-26

### Added
- **Public-share readiness hardening** (from a comprehensive readiness audit):
  CI now **compiles the Docker image** on every push/PR (`docker-build` job) so
  the container deploy can't silently break, runs on the same **Node 26 +
  corepack** as the image, and gained least-privilege `permissions`, a
  `concurrency` cancel, a pnpm cache, and Codecov upload + badge. Added
  **CodeQL** (python + js/ts) and **dependency-review** workflows; enabled
  secret-scanning / push-protection / Dependabot security updates. Fixed a
  shipped prompt that hardcoded the owner's name (now `{user_name}`), made the
  `ab_brief --run` flakiness honest in its docstring, anchored the prompt
  scorer's tone check to the enumerated schema block, and corrected doc drift
  (tool count 25→27, stale "43% gate", `.env.example` allow-list comment) +
  a `docs/` index. Added the repo's **first frontend tests** (Vitest + 5
  auth-path cases, run in CI) and a regression net over `briefing.py`'s
  streaming loop (30→82%), lifting total coverage to ~93%.
- **`chart` `line` style — a clean box-drawing line chart.** Drawn with 1-cell
  box-drawing glyphs (`─ ╭ ╮ ╰ ╯ │`) that connect into a smooth curve, with a
  y-axis + baseline. Two things keep it clean rather than stair-stepped: the
  series is **heavily smoothed** (centered moving average scaled to the window)
  and **down-sampled to a lower column count**, so each change is a gentle slope
  instead of a one-column riser. Box-drawing renders reliably everywhere (a
  braille prototype was smoother in principle but font-dependent, so it was
  dropped). Monochrome by design — a colored line needs chunky double-width
  emoji; `calendar` is the style for color. (`agent/charts.py` `render_line`.)
  (Earlier emoji / braille / under-smoothed line prototypes from this same
  `[Unreleased]` cycle were replaced.)

### Fixed
- **Calendar chart alignment.** The heat-grid mixed cell widths — ASCII `· `
  pads and an `M T W T F S S` header are narrower than the double-width emoji
  squares, so columns didn't line up. Every grid cell is now a single emoji
  (`⬛` for out-of-window days instead of dots), and the un-alignable ASCII
  weekday header is dropped in favor of a `rows = weeks (Mon→Sun)` note in the
  legend. Rows are now uniform 7-cell weeks that align cleanly. Regression test
  asserts every grid row is exactly 7 emoji cells with no ASCII pad.

## [0.14.0] - 2026-06-26

### Added
- **`chart` calendar style — the new default, fixes the truncation/compression
  bug.** Multi-week colored charts were rendered one row per day, so a 60-day
  window was 60 lines that the terminal truncated to a cramped ~14-line slice.
  The new `calendar` style (`agent/charts.py` `render_calendar`) lays the data
  out as a week-stacked emoji heat-grid — one colored square per day, weeks
  stacked top→bottom, Mon→Sun left→right — so any window stays compact (90 days
  ≈ 13 rows) and fully visible. Missing in-window days render as ⬜; the
  right-hand weekly column is a sum for additive metrics (steps, intensity
  minutes) and the mean of present days otherwise. `calendar` is now the tool
  default; `bar` (one row per day) remains for short ≤2-week windows, alongside
  `combo` and `spark`.

## [0.13.0] - 2026-06-25

### Added
- **`chart` MCP tool — terminal graphs from your data.** A new read-only tool
  (`mcp__fitness__chart`) renders any daily metric, the training-load series
  (`ctl`/`atl`/`tsb` = fitness/fatigue/freshness), or the derived
  `intensity_minutes_weighted` (Garmin "active minutes" = moderate + 2×vigorous)
  as a terminal chart. Three styles: `bar` (emoji-color horizontal, default),
  `combo` (2D vertical bars with a least-squares trend line overlaid — handles
  negative series like TSB), and `spark` (one-line sparkline). A prototype
  against the live terminal established that ANSI color is stripped on the way
  to the display, so color is carried by emoji glyphs, not escape codes. The
  renderers live in `agent/charts.py` as pure functions. The tool is available
  to the chat/coach loop but deliberately excluded from the brief's tool set
  (the brief renders its own UI cards), mirroring the `daily_snapshot`
  precedent.

### Changed
- **Test coverage raised 65% → ~90%** with mock-free tests (real tmp SQLite +
  hand-rolled fake Garmin clients + `ASGITransport`/`CliRunner`) for the
  previously-thin I/O edges: `ingest/backfill.py` (8→100%), `ingest/daily.py`
  (10→92%), `web/server.py` (55→92%), `web/mcp_server.py` (71→95%), `cli.py`
  (39→81%), `ingest/auth.py` (25→86%), plus `agent/briefs.py`/`units.py`/
  `status.py` to 100% and `briefing.py`'s pure partial-JSON parser. The SDK
  message-stream and uvicorn/transport glue are left untested on purpose
  (YAGNI — those tests would only assert mocks replay themselves). CI
  `--cov-fail-under` raised 43 → 85 to lock in the gain.

### Removed
- **YAGNI cleanup for the public repo** (~400 LOC): the three one-off
  `scripts/phase0_*.py` probes; dead code in `server.py` (`BRIEFINGS_DIR`,
  a duplicate of `briefs._default_briefings_dir`), `tools.py`, `coach.py`,
  `ingest/auth.py` (`clear_credentials`); an orphaned `StatCard.tsx` + unused
  `deltaText` in the web app; and four unused single-knob `config.py` accessors.

### Fixed
- **Repo tidied for public consumption**: dropped the owner's LAN host
  `fitness.home.local` from the shipped `_DEFAULT_ALLOWED_HOSTS` default
  (now `127.0.0.1,localhost`; add your own via `LOCAL_FITNESS_MCP_ALLOWED_HOSTS`);
  clone-agnostic `./data` volume examples in `docs/deployment.md`; a header
  marking root `CLAUDE.md` as maintainer-internal.

## [0.12.1] - 2026-06-25

### Fixed
- **Frontend rendered a black screen — `react` and `react-dom` versions had
  drifted apart.** Dependabot's react bump (#15) moved `react` to 19.2.6 but
  left `react-dom` at 19.2.5. React 19 requires the two packages to be the
  exact same version and throws *Minified React error #527* on mount when they
  differ, so the SPA mounted nothing and left the dark `bg-bg` background
  showing. `tsc -b` + `vite build` never execute the app, so CI stayed green
  and the broken bundle shipped to the container. Pinned both packages to
  19.2.7 so they move in lockstep. Verified the live container renders via a
  headless-Chrome DOM probe (root no longer empty; only the expected auth-gate
  401 remains).

## [0.12.0] - 2026-06-24

### Changed
- **Adopted a `feature/* → dev → main` branch model** (mirrors the
  `natejswenson.io` workflow, adapted for a public repo). `main` is the
  default/production branch and `dev` is integration; both are protected
  (CI `validate` green + a PR required, linear history, squash-only,
  branch auto-deleted on merge, 0 required reviews so a green PR
  self-merges via native auto-merge). `enforce_admins` is off as a
  deliberate solo-dev break-glass path. The old `master` branch was
  renamed to `main`.
- **Release is now auto-tagged on a `dev → main` promotion.** `release.yml`
  stays version-driven and is retargeted to `[main]`: a promotion that
  bumps this version (with a matching CHANGELOG entry) auto-cuts the tag;
  a no-bump promotion is an idempotent no-op. CI runs on `[main, dev]`.
- **Dependabot now targets `dev`** on all four ecosystems, so dependency
  bumps flow through the same promotion path.

### Fixed
- **Container build under `node:26`**: the base-image bump dropped the
  bundled `corepack` shim, breaking `corepack enable`. Install
  `corepack@latest` explicitly in the web-builder stage. (CI didn't catch
  it — CI runs `pnpm build` on the host, not the Docker image.)

## [0.11.0] - 2026-06-23

### Added
- **The coach profile now carries into tool-driven Claude Code chat**, not just
  the `/mcp__fitness__coach` slash command. The fitness MCP server advertises the
  resolved coach persona as its top-level `instructions`, so when Claude Code
  answers a fitness question by calling the MCP tools (rather than the slash
  command), the reply adopts your selected `coach_profile`'s voice.

  Resolution is **live, per client connect** (a `create_initialization_options`
  wrap): a `fitness config set coach_profile …` takes effect on the next
  connect — no server restart, consistent with the slash-command prompts. The
  wrap is **import-safe** (it does no DB I/O at server-build time, which on the
  HTTP path runs before the schema is initialized) and **fail-open** (a
  resolution error advertises no persona rather than breaking the MCP handshake).
  `fitness mcp-stdio` now initializes the schema for parity. Reuses the existing
  `system_prompt` unchanged (no prompt edit; `score_prompt.py` untouched).

  Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-23-mcp-server-coach-persona-design.md`); the gate caught a
  clone-breaking import-time crash in the first approach.

## [0.10.0] - 2026-06-23

### Added
- **Selectable coach tone profiles for the daily brief.** The coaching voice is
  now a profile you pick instead of one hardcoded blend:
  - `supportive` — always upbeat and encouraging; frames every read as a
    bounce-back, never roasts;
  - `neutral` — emotion out of it, tells you how it is against your goals;
  - `hardass` — cynical and relentless; rips you for anything short of
    overachieving and always pushes for more;
  - `adaptive` *(default)* — today's "supportive when trending well, roast when
    slipping" behavior, unchanged for a fresh clone.

  Each profile is a fully-fleshed `agent/coach_profiles/<name>.md` (voice body +
  numeric dials) with tunable characteristics: `harshness`/`warmth`/`push` (0–10
  prose calibration) and `roast_threshold`/`praise_threshold` (fractions of goal).
  The thresholds carry **deterministic** behavior — for goal-based mandates
  (steps, plan adherence) a harsh profile assembles the harsh-tone imperative
  block and a soft profile omits it (gated on `harshness`), which is unit-tested.
  Select with `uv run fitness config set coach_profile hardass` or
  `LOCAL_FITNESS_COACH_PROFILE`; override any dial (`coach_harshness`, …) the same
  way — resolution is settings DB > env > the profile's own value.

  Verification (every profile against expected outcomes, not eyeballed): a new
  deterministic `scripts/score_profiles.py` (27 checks, CI-gated via
  `test_coach.py`) asserts each profile keeps the schema/tone/jargon contracts
  and that the harsh-block gating is correct per profile; `scripts/ab_brief.py
  --profile <name>` runs a generative A/B per profile; the adaptive default's
  cross-model A/B is `consistent` and `score_prompt.py` stays green unchanged.
  Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-23-coach-tone-profiles-design.md`).

## [0.9.0] - 2026-06-23

### Added
- **Grading and projection behavior is now user-configurable** instead of
  hardcoded to one runner's preferences. Five knobs, each defaulting to the
  previous hardcoded value (so a fresh clone is unchanged):
  - `count_walks_easy` (default `true`) — do recovery walks satisfy an
    easy/recovery prescription;
  - `count_walks_mileage` (default `false`) — include walking in the weekly
    mileage rollup;
  - `grade_done_fraction` / `grade_partial_fraction` (`0.80` / `0.40`) — the
    done/partial grade bands;
  - `riegel_lookback_days` (`120`) — lookback window for the projected finish.

  Resolution precedence is **settings DB > env var > default**: set a value live
  with `uv run fitness config set <key> <value>`, or in `.env`
  (`LOCAL_FITNESS_COUNT_WALKS_EASY`, etc.; documented in `.env.example`). Values
  are validated — a blank or unrecognized value falls back to the default, and an
  inverted fraction pair (`partial > done`) or out-of-range fraction reverts both
  to defaults so the grade bands can't invert; a nonsense lookback clamps to the
  default. A new `config.py` accessor resolves the knobs; a `GradingConfig`
  dataclass threads them into the (still pure) grading functions in `plans.py`,
  resolved once per request by the brief, the plan tool, and the web plan route —
  so the brief and the tab grade consistently. Designed and `/quality-gate`-
  reviewed first (`docs/plans/2026-06-23-configurable-grading-design.md`).

## [0.8.0] - 2026-06-23

### Fixed
- **Training-plan grading: a completed workout today no longer shows
  `pending`.** `grade_workout` is now outcome-based — it grades first and holds
  `pending` only when the verdict is negative (`missed`/`partial`) AND the day's
  data window is still open. A synced workout grades immediately, even today;
  rest days resolve to `compliant` instead of lingering `pending`. (Holding
  `partial` too prevents a mid-day half-done run from prematurely counting 0.5
  in adherence and then self-healing.)
- **A recovery walk is now reflected in the plan.** Easy/recovery days count
  walking distance toward the prescription (active recovery is the intent);
  `long`/`tempo`/`interval`/`race` stay running-only. Per-workout actuals are
  now foot-based (running + walking) on every day and carry a normalized
  `actual_activity_types` (e.g. `["walking"]`), so a walk is visible regardless
  of verdict. The plan tab's Actual-cell coloring is now driven by the backend
  `verdict` (red only when `missed`) instead of recomputing a pace/distance
  miss — so a walk-counted `done` day no longer paints red on walking pace.
  Weekly mileage intentionally stays running-only (it's a run-volume metric,
  distinct from recovery-day adherence).

Designed and `/quality-gate`-reviewed first
(`docs/plans/2026-06-23-plan-grading-fixes-design.md`; 4 rounds + look-harder,
5→0). Frontend coloring verified by screenshot of the plan tab.

## [0.7.0] - 2026-06-22

### Added
- **`get_training_plan_progress` MCP tool** — returns the full graded training
  plan day-by-day (every prescribed workout with its verdict:
  done/partial/missed/compliant/pending), plus goal, days-to-race, adherence %,
  and projected finish. Fills the gap that previously forced ad-hoc `sqlite3`
  spelunking to answer "show my plan through today". Implemented as a deliberate
  projection over `build_plan_detail` with a no-active-plan guard and a
  `.get`-hardened `days_to_race`; kept out of the brief's read-only allow-list
  so the brief stays cheap. Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-22-fitness-qa-clean-output-design.md`).

### Changed
- The shared chat-formatting block in `system_prompt()` now tells the agent to
  prefer the structured `mcp__fitness__*` tools, never shell out to
  `sqlite3`/Bash for a DB read, and present answers cleanly instead of narrating
  the lookup. Mirrored as a new "Answering fitness questions" section in
  `CLAUDE.md` for the in-repo Claude Code surface. (Verified: static prompt
  scorer green; the edit lives in the chat-only block and introduced no new
  brief A/B divergence — the `ab_brief.py` `_generate` path fails identically
  with and without the edit due to a pre-existing harness flake, unrelated to
  this change.)

## [0.6.0] - 2026-06-20

### Changed
- **Daily brief is ~2.5–3× faster (~230s → ~82–97s) with equal-or-better
  quality.** Measurement (`scripts/phase0_*`) found the brief's wall-clock is
  dominated by extended thinking (~93% of output tokens), not tools or
  round-trips. The SDK `thinking.budget_tokens` knob is ignored on the Claude
  Code CLI / Max-OAuth path, but reasoning `effort` propagates — so the brief
  composer now runs at `effort="low"` by default. A blind LLM-judge A/B rated
  low-effort briefs as good or better than the prior default on specificity,
  coach-voice, non-repetition, and no-dead-weight. Tunable via
  `LOCAL_FITNESS_BRIEF_EFFORT` (low|medium|high|max).
- A fan-out (map-reduce) architecture was designed and quality-gated, then
  **rejected by Phase 0 measurement** (concurrent `query()` parallelizes only
  1.44× at 3-wide, under the 1.7× kill criterion). Design + outcome retained in
  `docs/plans/2026-06-19-brief-fanout-and-cli-ux-design.md`.

### Added
- **Deterministic table rendering.** Shared `agent/render.py` `render_table`
  (now the single source for the coach snapshot table in `mcp_server`) and a
  `fix_table_row_breaks` repair applied at the brief save gate — eliminates the
  collapsed-markdown-table defect (`|---|---|n| RHR |`) the model emits more
  often at lower effort.
- Brief token-usage instrumentation (`brief_usage` log line) for latency
  attribution.

## [0.5.0] - 2026-06-18

### Changed
- **Agent-first architecture.** The web-server process no longer runs any
  Claude inference. All synthesis — the daily brief, conversational coaching,
  plan drafting/revision, dashboard insights — moves to a client agent (Claude
  Code / Desktop / Mobile) talking to the fitness MCP. The server keeps the
  deterministic compute (baselines, CTL/ATL/TSB, plan grading, status) and
  serves it over REST + MCP. The UI reads the same data as before; what changes
  is *who writes the brief* and *where you converse with the coach*.
- **Single brief write gate.** New Claude-free `agent/briefs.py` owns brief
  I/O; `save_brief()` is the one validate-and-atomic-write path, shared by the
  scheduled composer, the new `save_brief` MCP tool, and `ab_brief.py`.
- **`/api/brief` falls back to the most recent brief** (`load_latest()`) when
  today's hasn't been written, so the Today tab never goes empty while any
  brief exists. The stale-brief banner is now informational.
- **The UI is a viewer.** Today shows the agent-written brief (no Generate
  button, no embedded chat); Training Plan reviews + commits a draft the agent
  writes (drafting moves to the MCP client); Dashboards keep every chart and
  range toggle but drop the per-panel insight chats and the model toggle.

### Added
- **`brief` MCP prompt** + `save_brief` MCP tool, so an MCP client can compose
  and persist a brief through the same integrity gate the scheduled job uses.
- **`GET /api/plan/draft`** — lets the plan viewer show a pending draft without
  a chat surface.
- **`ops/` launchd job** (`install-launchd.sh` / `uninstall-launchd.sh` +
  plist template) that runs the daily `fitness brief` composer at 06:30 with
  next-wake catch-up. Documented `CLAUDE_CODE_OAUTH_TOKEN` (needed only by the
  scheduled composer, not the server) in `.env.example` + `docs/deployment.md`.

### Removed
- The server-side Claude loops: `agent/chat.py`, the `/api/chat*` and
  `/api/brief/generate*` endpoints, the `chat`/`ask` CLI commands, and the
  `ChatPanel` / `DashboardInsight` frontend components.

### Security
- **`run_sql` is now read-only at the SQLite engine, not by keyword matching.**
  The MCP `run_sql` tool opens a `mode=ro` connection (`db.connect_readonly`), so
  any INSERT/UPDATE/DELETE/DDL fails at the engine regardless of phrasing. This
  closes a bypass where a `WITH`-prefixed query with a newline/tab after the
  write keyword (`WITH a AS (SELECT 1)\ndelete\nfrom …`) slipped the prefix and
  space-padded-keyword denylist and committed. The denylist stays as
  defense-in-depth.
- **`run_sql` is time-bounded and non-blocking.** A `set_progress_handler`
  deadline (5s) aborts runaway queries with a clean error, and execution is
  offloaded via `asyncio.to_thread`, so a heavy query can no longer freeze the
  single-threaded server (authenticated DoS).
- **MCP tools validate window/numeric inputs.** Date-window tools
  (`get_metric`, `get_metric_trend`, `query_workouts`, `find_anomalies`,
  `recovery_pattern`, `correlate`, `list_observations`) reject out-of-range /
  non-int `days`/`lookback_days`/`lag_days` via `_validate_days` instead of
  raising `OverflowError`; the plan validator rejects wrong-typed workout fields
  with clean indexed errors instead of `TypeError`/`AttributeError`.
- **`_is_public_path` is case-normalized** so an uppercase `/API/…` can't be
  treated as a public (SPA) path while bypassing the lowercase `/api/` gate.
- `run_sql` no longer echoes raw SQLite exception strings.

### Fixed
- **Stale-brief banner could never clear in the evening.** The server runs in
  UTC, so its daily pull writes a `daily_metrics` row for "tomorrow" once UTC
  rolls over — making `data_through_date` one day ahead of a just-written
  brief's local date, so `isBriefStale` stayed true forever. The banner now
  clamps the data frontier to the *viewer's* local day: a row for a day that
  hasn't finished in your timezone isn't "newer data." Genuinely stale briefs
  still flag.
- Container build: build the SPA on Debian (glibc) instead of Alpine (musl) and
  pin pnpm so Vite 8's rolldown native binding installs; harden uv/pnpm fetch
  against a flaky build network.

### Added
- **"Ask your coach" is now an actionable button.** The brief banner, the
  empty-brief state, and the empty training-plan state each copy a ready-to-paste
  MCP prompt to the clipboard (a web page can't launch a Claude client, so it
  hands you the prompt to paste into Desktop / Code / Mobile).

## [0.4.0] - 2026-06-17

### Added
- **Training plans.** A `/plan` tab where you pick a goal (5K / 10K / Half /
  Full / Custom), a race date, and a target time; the agent drafts a periodized
  plan from your Garmin history, you riff with it in chat, and commit it. The
  committed plan is tracked (goal header with a Riegel predicted finish,
  schedule with per-day adherence, **Target/Actual** distance + pace columns,
  planned-vs-actual weekly mileage, CTL trajectory) and folded into the daily
  brief's workout takeaway (recovery takes precedence over the schedule on
  red-flag days). The Today tab shows a **Today's Goal** card read
  deterministically from `/api/plan`.
- Two tables (`training_plans`, `plan_workouts`) with a partial unique index
  enforcing a single active plan at the DB level.
- Three DRAFT-ONLY agent tools (`propose_training_plan`, `revise_training_plan`,
  `get_training_plan_status`) — the agent only writes drafts; activating or
  deleting a plan is a human action via REST (`GET /api/plan`,
  `POST /api/plan/{id}/commit`, `DELETE /api/plan/{id}`).
- `plans.score_plan` — a deterministic plan-quality gate (safe ≤15%/week ramp +
  taper into the race).
- `scripts/ab_brief.py` — a cross-model A/B simulation harness for prompt
  changes (dry-run by default, hard generation cap, cost-free `--mock` mode).
- A `Content-Security-Policy` header (`script-src 'self'`) as defense-in-depth
  against XSS from AI-authored plan strings.

### Notes
- Integrates the training-plans feature (previously the unmerged
  `design/training-plans` branch) alongside the MCP work from 0.2.0–0.3.1.
  Adherence is computed from the activities join (immune to plan-row edits) and
  graded against the data frontier so Garmin lag never shows a false "missed".
  The reverted brief-pre-fetch experiment from that branch is not included.

## [0.3.1] - 2026-06-17

### Fixed
- **`notes.append_note` return contract** — it hardcoded `line=-1`, so
  `save_user_note` reported the wrong index and a follow-up update/delete using
  it silently no-op'd. Now returns the index `read_notes()` assigns.
- **Manual-workout partial-failure / duplicate-on-retry** — the row committed,
  then `baselines.recompute()` ran unguarded; a recompute failure raised as if
  the write failed, and a retry inserted a second negative-id workout,
  double-counting training load. Recompute failure now returns partial-success
  (`logged`/`deleted: true, recompute_failed: true`). `log_manual_workout` also
  rejects non-positive duration and future dates; `log_observation` validates
  `observed_on` the same way.
- MCP `serverInfo` version + `__version__` now track the package version.

### Changed
- **Coach output renders cleanly in a narrow chat.** The shared `system_prompt`
  now carries an output-formatting contract steering every conversational reply
  (free chat *and* `/fitness:coach`) away from wide markdown tables (which wrap
  into mush in a monospace MCP pane) toward compact per-item lines and
  phase-grouped sections — e.g. a training plan renders as
  `Wk 5 · Jul 13 · Build · long 8mi · threshold 4×6min` lines, not a 6-column
  grid. Scoped to conversational prose only; the structured JSON brief and its
  schema are unchanged (prompt scorer still 11/11).

## [0.3.0] - 2026-06-16

### Added
- **`coach` MCP prompt** — `/fitness:coach` in an MCP client assembles the full
  daily snapshot (per-metric vs-baseline read, training load, recent workouts in
  miles) *and* the coach persona + your saved notes server-side, in one
  round-trip with no tool-call latency or server-side Claude cost. The coaching
  synthesis that previously lived only in the brief loop now travels to the MCP.
- **`daily_snapshot` tool** — one call returns the assembled status (collapses
  the get_today_status + training_load_status + query_workouts + notes chain).
- **`fitness://schema` and `fitness://brief/latest` MCP resources** — the
  queryable-column reference (single-sourced from `QUERYABLE_SCHEMA`, so it can't
  drift from `run_sql`) and the most recent saved brief rendered to markdown.
- **Write surface** — `log_observation` / `list_observations` /
  `delete_observation` (RPE, soreness, weight, mood, feeling, injury, free
  notes; validated against `OBS_TYPES`, soft-referenced to a workout) and
  `log_manual_workout` / `delete_manual_workout` (non-Garmin workouts stored in
  `activities` with a negative synthetic id under `BEGIN IMMEDIATE` and
  `source='manual'`, feeding CTL/ATL/TSB via `baselines.recompute()` with a
  widened lookback so backdated workouts rewrite their own date's row).
- **Server-side miles** — `query_workouts` / `get_workout_detail` / the snapshot
  add `distance_mi`, `pace_min_per_mi`, and formatted duration alongside the raw
  values (`agent/units.py`). `LOCAL_FITNESS_DISPLAY_UNITS` (default `miles`).

### Changed
- Brief generation is now restricted to a read-only tool allow-list
  (`read_only_tool_names()`), so it structurally cannot invoke a write tool; its
  tool set is otherwise unchanged. Chat and the web agent keep the full set.

### Database
- New `observations` table (idempotent DDL; `activity_id` is a soft reference,
  no enforced FK) and a guarded `activities.source` column; `init_schema()`
  stays idempotent across calls.

## [0.2.0] - 2026-06-16

### Added
- **MCP server** — the fitness tools are now reachable from interactive Claude
  sessions (Claude Code / Desktop / other local agents) over the Model Context
  Protocol. Deployed endpoint at `/mcp/` (streamable-HTTP, behind the existing
  `LOCAL_FITNESS_API_TOKEN` bearer gate); local `fitness mcp-stdio` for
  auth-free laptop use. Connect: `claude mcp add --transport http fitness
  https://fitness.home.local/mcp/ --header "Authorization: Bearer $TOKEN"`.
  Implemented by reusing the SDK's already-built tool `Server`
  (`web/mcp_server.py`) over a new transport — one source of truth, no schema
  or handler duplication, so it auto-tracks `agent/tools.py::ALL_TOOLS`.
- **`LOCAL_FITNESS_MCP_ALLOWED_HOSTS`** env var — host allowlist for the MCP
  transport's DNS-rebinding guard (must include the served host or `/mcp/`
  returns 421). Defaults to `fitness.home.local,127.0.0.1,localhost`.

### Security
- `/mcp` and `/mcp/*` are explicitly auth-gated in `_is_public_path` (they live
  outside `/api/`, which defaults to public) — regression-tested in
  `tests/test_security.py`.

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
