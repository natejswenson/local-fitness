# Changelog

All notable changes to local-fitness are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.47.0] - 2026-08-03

### Fixed
- **The `/coach` prompt leaked raw float64 that every tool payload rounds
  away.** `_round_floats` describes itself as "the ONE choke point every tool
  payload flows through", but it only ran inside `_text`/`_err` —
  `mcp_server._render_status` formats `assemble_status()` straight to markdown
  and bypassed it. The same data reached the model two ways, live:

  ```
  /coach prompt:       CTL 58.957722002523454 · TSB -0.077230305434135
                       avg_stress ↓ -14.6% vs baseline 31.616666666666667
  daily_snapshot tool: ctl 58.96, tsb -0.08, baseline 31.62
  ```

  Now routed through the same helper. Raw float64 in a prompt is noise the
  model must re-round before it can speak, and it invites a spurious-precision
  read-back.

- **A prescribed HR cap was bounded on edit but not on create.**
  `update_plan_workout` has rejected anything outside 90–210 bpm since 0.40.0;
  `plans.validate_plan_input` — how a plan is *created* — only checked
  finite-and-non-negative, so `target_hr_max: 14` was rejected on an edit and
  accepted on a proposal. The blast radius is the whole plan, silently: the
  report card grades HR via `hr_cap_severity = (bpm_over − 1.5) / 28`, so a
  14 bpm cap puts every capped day ~120 bpm over the ceiling → F on HR → the
  F-cap drops each of those days to C for the life of the plan, with no error
  anywhere. The bounds now live once in `plans.MIN_PRESCRIBED_HR` /
  `MAX_PRESCRIBED_HR` and both write paths read them. 90 is deliberately not
  tightened further — it is a plausible real recovery-run ceiling.

### Tests
- **The three `run_sql` boundary tests would have passed a `run_sql` that
  rejected every query, valid ones included.** Each asserted only `assert err`,
  so none could tell "rejected correctly" from "rejected for the wrong reason".
  They now pin the distinguishing message — `only SELECT/WITH queries
  permitted`, `forbidden keyword: update`, `no such table: does_not_exist` —
  and a fourth test asserts a valid `SELECT` still *succeeds*, which is the
  half whose absence made the reject-everything implementation pass.

## [0.46.0] - 2026-08-03

### Added
- **`update_plan_workouts` — re-prescribe many days on the active plan in ONE
  atomic call.** Measured across recorded sessions: 39 `update_plan_workout`
  calls, ~20 of them a single restructure, and 52 fitness tool calls in one
  day. Two costs, and the second is the real one:

  - 20 model turns, each carrying the full tool schemas and persona.
  - **20 independent transactions.** The documented "move Saturday's long run
    to Sunday" idiom is *two* calls — rest the old day, prescribe the new one.
    If the second failed, the long run was simply gone, with nothing to roll
    back.

  One batch is one transaction. Validation runs in three passes before any row
  changes: per-entry shape through the same `_prescription_fields` the
  single-day tool uses, then the `_EDITABLE_WORKOUT_COLS` whitelist plus a
  duplicate-`(date, seq)` check, then an existence pre-flight `SELECT` for
  every target. A typo'd date aborts before the first write rather than
  half-applying, and every error names the offending entry by index and date.

  **Not a latency fix.** 20 sequential calls cost ~75 ms of our time end to end
  (~3.8 ms each); the LLM turns dominate by three orders of magnitude. The win
  is turns, tokens and atomicity — the docs say so explicitly so nobody
  justifies it on database performance later.

  The write boundary is unchanged: same whitelist, same keyed `UPDATE`, `date`
  is still the key and not an editable column. A batch is many
  re-prescriptions, never a restructure. Capped at 60 entries as a blast-radius
  bound (the live active plan is 75 workouts).

### Changed
- **The rest-day clear moved from the tool to the write boundary**
  (`plans.apply_rest_semantics`). It cleared distance/pace/duration/`hr_max`
  inside `update_plan_workout`, which was correct only because that tool was
  the sole caller — the moment a second write path existed, a caller that
  skipped it would leave a stale HR cap on a rest day, prescribing a session
  that no longer exists and that the report card grades against directly. Now
  every caller of `update_active_workout(s)` inherits it. Single-day behaviour
  is byte-identical, pinned by a regression test.
- The persona's plan section gains the batch tool ("Never loop
  `update_plan_workout` over a list of days"). Persona is 12,303 chars, still
  under the 13,000 ceiling.

### Fixed
- `update_plan_workout`'s `hr_max` description and `docs/mcp/update_plan_workout.md`
  both said the report card grades "time-above-cap" against the column. 0.40.2
  replaced that axis with `hr_exceedance_bpm` precisely because a time fraction
  fed into bands calibrated for relative magnitudes was a category error; the
  fraction survives as reporting only. The docs page also listed
  `_EDITABLE_WORKOUT_COLS` as five columns, omitting `target_hr_max` — added in
  0.40.0. `test_docs_drift.py` checks page existence and availability claims,
  never body accuracy, so this class of drift is unguarded.
- `docs/mcp/README.md` described `get_training_plan_draft` as "the only way to
  see a pending draft"; 0.44.0 made `get_training_plan_status` report one.

## [0.45.0] - 2026-08-03

### Changed
- **The coach persona now has a training-plan section, paid for by two that
  pointed at tools nobody calls.** Measured across every recorded session (247
  real tool invocations), the persona's token budget was allocated backwards:

  | persona section | tokens | tools it orients toward | real calls |
  |---|---|---|---|
  | Managing preferences conversationally | 532 | `save`/`update`/`delete_user_note` | 0 |
  | Writing your journal | 199 | `save_coach_memory` | 0 |
  | — no section existed — | 0 | every plan tool | **62 of 247** |

  Both cut sections are superseded rather than merely unlucky:
  `data/user_notes.md` was last written 2026-06-18 (preferences migrated to
  `update_coach_personality`, 25 calls), and 0 of 18 `coach_journal` entries
  came from chat — 14 `report_card`, 4 `brief`, all auto-reflect. Neither was
  deleted; both were compressed to the decision rule, keeping every tool name.

  The new section carries the four constraints an agent cannot recover from
  the tool descriptions alone, because each spans two tools: `update_plan_workout`
  edits ONE existing day so a swap is two calls; pass `hr_max` because a cap
  written only in prose is invisible to the grader; restructuring goes through
  a draft, never day-by-day patching; and a non-null `pending_draft` must be
  closed because the next proposal silently archives it.

  Net effect: the persona **shrank** 14,920 → 12,016 chars (~726 tokens saved)
  while gaining the guidance for its largest usage cluster. It is delivered on
  every `/coach` invocation, so this is a per-session saving.
  `test_system_prompt_stays_under_its_size_ceiling` is a ratchet against
  quietly growing it back.

### Fixed
- **The MCP server reported itself as `fitness v0.6.0`.** `make_server()` had
  the version hardcoded as a literal since the server's first commit, so by
  0.44.0 every client — Claude Desktop, opencode, a phone over `/mcp/` — read
  a `serverInfo` 38 releases stale. That is the number you check to decide
  whether a fix has shipped. `tools.server_version()` now reads installed
  package metadata, falling back to `"0.0.0"` when the package isn't installed
  (a version string must never stop a server from starting).
  `test_server_version_tracks_pyproject` compares against `pyproject.toml`
  parsed directly — asserting against `importlib.metadata`, which is what the
  code itself reads, would be a tautology that passes whatever either says.

### Tests
- `test_server_and_tool_names` asserted `server is not None`, which passes for
  any object at all. It now pins `server["name"] == SERVER_NAME == "fitness"`.

## [0.44.0] - 2026-08-03

### Added
- **`get_training_plan_status` now reports `pending_draft`.** A proposed plan
  that was never committed governs nothing and was invisible on every path an
  agent had reason to call: `get_training_plan_status` and
  `get_training_plan_progress` both read the ACTIVE plan only, and the one
  tool that could see a draft — `get_training_plan_draft` — was never called
  once across every recorded session. Since `plans.insert_draft` archives any
  existing draft, an unsurfaced draft is destroyed by the next proposal
  without a word.

  Measured on the live DB: a 59-workout draft ("10K sub-48:30 — walk-supported
  rebuild") sat unnoticed for 12 days from 2026-07-22 while the active plan
  was hand-patched one day at a time — 39 single-day `update_plan_workout`
  calls, 52 fitness tool calls in one day.

  `plans.draft_summary()` is pure and returns
  `{plan_id, title, created_at, workout_count, first_date, last_date}` or
  `None`. A **summary, not the plan** — this tool's contract is "slim by
  design" and the full draft has its own tool.

  Resolved BEFORE the no-active-plan early return, so the key is present on
  both branches: "nothing active, a draft waiting" is exactly the state worth
  reporting, and a bare `{"active": false}` hid it. Read on the connection the
  handler already holds (`plans.get_draft_plan` gains the `conn=` parameter
  `get_active_plan` has had), so this costs **no extra `db.connect()`** — the
  tool is on the perf gate's hot path. Measured A/B over 300 iterations
  against the synthetic fixture: **0.650 ms with vs 0.642 ms without, +1.2%**,
  against a 15%-of-min floor.

### Changed
- `docs/mcp/get_training_plan_status.md` said "Drafts are invisible here" in
  four places. All four now describe the draft as a loose end to close, and
  name the silent-archive consequence of leaving one open.

### Security
- **A crafted `Host` header could skip the bearer gate entirely.** The auth
  middleware decided public-vs-private from `request.url.path`. Starlette
  rebuilds `request.url` out of the `Host` header
  (`f"{scheme}://{host}{path}"`) and re-parses it, so a `/` in that header
  moves the path boundary: a `POST /mcp/` sent with
  `Host: fitness.home.local/health#` presented a `url.path` of `/health`,
  `_is_public_path` returned True, and `require_api_token` returned before
  checking the token (starlette <1.0.1, GHSA-86qp-5c8j-p5mr).

  `_is_public_path` was never wrong — the bug was what got *passed* to it.
  Both middlewares now read `_request_path(request)` → `request.scope["path"]`,
  the string the ASGI server parsed from the request line and the one the
  router actually dispatches on. No header can influence it, so the gate and
  the router cannot disagree.

  **What was containing it, and why that wasn't good enough.** The request
  still died at 421 in the MCP transport's own DNS-rebinding guard, which
  compares the Host against its allowlist *exactly* — and a poisoned Host,
  containing a `/`, can never match. That is an accident of the attack's
  shape, not a control. Under the supported wildcard-port form
  (`LOCAL_FITNESS_MCP_ALLOWED_HOSTS='fitness.home.local:*'`) the guard's
  `startswith` branch accepts it and the identical unauthenticated request
  completed a full MCP `initialize` — and from there `tools/list` and every
  read and write tool. Traefik filters the proxied path but not traffic
  reaching the container directly on the Docker network.

  Fixed in two independent layers: the `scope["path"]` change (holds
  regardless of dependency version) and the starlette bump below (closes
  today's CVE). The code change is the durable one — bumping alone would fix
  this instance and leave the class open.

- **Dependency bumps for 6 HIGH advisories.** `starlette` 1.0.0 → 1.3.1
  (the above, plus the HIGH form-limit DoS GHSA-82w8-qh3p-5jfq),
  `python-multipart` 0.0.26 → 0.0.32 (quadratic-querystring DoS, unbounded
  multipart headers), `pydantic-settings` 2.14.0 → 2.14.2.

### Tests
- Three cases in `tests/test_security.py`. `test_poisoned_host_header_cannot
  _bypass_the_bearer_gate` fails on the pre-fix middleware by raising
  `RuntimeError: Task group is not initialized` from inside the mounted MCP
  app — reaching the mount at all is the proof the gate was skipped.
  `test_request_path_is_invariant_under_any_host_header` pins the invariant
  across five hostile Host forms and is deliberately version-independent: an
  earlier draft asserted that `url.path` *does* relocate, which the starlette
  bump immediately falsified, turning the test into a CVE detector that
  silently passes once the library is patched.
  `test_health_stays_public_with_an_ordinary_host_header` guards the
  over-correction — the container's liveness probe must stay unauthenticated.

## [0.43.0] - 2026-08-02

### Added
- **`scripts/calibrate_report_card.py` — the check that would have caught the
  0.40.0 HR-cap defect.** CLAUDE.md has said "calibrate bands against real data,
  not intuition" since 0.26.0 and nothing executed it, which is why every
  grading defect in this module's history was found by a human reading a
  rendered card. The script recomputes every graded metric across a trailing
  window through the real production path
  (`load_report_card_inputs` → `build_card`, never a reimplementation), prints a
  per-metric letter histogram, and exits non-zero on either degeneracy
  signature: **punitive skew** (>60% of runs graded D/F) or **dead bands** (≥2
  letters never used).

  Verified against the defect it was designed for. On `23ee63a` — the commit
  where a human investigation looked at this exact card and concluded "the grade
  was fine" — it reports `hr (prescribed cap)  1 0 0 0 9  FAIL — 90% of runs
  graded D/F`. On the corrected rubric the same line reads `3 0 3 1 3  ok — 4/5
  bands used`, and the F-cap rate over the window falls 21% → 12%.

  The asymmetry between the two signatures is deliberate and was the correction
  to this check's own first draft, which failed any letter above a flat 60%
  share: distance grades 79% A because the distances are being hit. A rubric
  measures compliance with a prescription the athlete is *trying* to follow, so
  concentration in a passing grade is evidence of nothing and is reported rather
  than gated.

  Strictly read-only (`mode=ro` URI — SQLite refuses the write rather than the
  script merely avoiding it), and deliberately **not** in CI: it needs a
  populated database CI does not have, and a fabricated one would only ask the
  fixture whether it agrees with itself. `test_the_gate_is_not_wired_into_ci`
  fails if someone adds it to a workflow.

- **`tests/evals/report_cards.py` + `tests/evals/test_report_card_verdicts.py` —
  verdict evals for the report card.** The report card had 141 unit tests and no
  evals. Those tests assert that the rubric computes what it says it computes —
  a deviation float, a band boundary, a display string — and not one of them
  could fail when the rubric's *answer* was wrong. These assert the **overall
  letter a run deserves**, through a fabricated SQLite database and the full
  `load_report_card_inputs` → `build_card` path, so the reference-pool filter is
  inside what they grade.

  Five scenarios, each a failure that reached a rendered card:
  `obedient_easy_clean`, `obedient_easy_straddling` (the 2026-08-02 shape —
  average obeys a 140 ceiling while 60% of the run sits 1–3 bpm over),
  `cap_blown_hard`, `interval_manual_laps`, `walk_mislabelled`. Expected
  verdicts are declared as BOUNDS in `EXPECTED_VERDICTS`, with a stated reason;
  a scenario without an entry fails `test_every_scenario_declares_a_verdict`.

  Verified against `23ee63a`: three tests fail there, including
  `test_the_three_cap_scenarios_are_strictly_ordered` with `assert 2.0 > 2.0` —
  straddling a cap by a beat and blowing it by 15 bpm both graded C, which is
  the collapse the ordering test exists to name.

### Fixed
- **`tests/conftest.py` puts `tests/evals/` on `sys.path`** alongside `scripts/`,
  so a test outside that directory can import the fixture builders. This is what
  lets `tests/test_calibrate_report_card.py` reuse the report-card scenarios
  instead of fabricating a second corpus that could drift from them.

## [0.42.0] - 2026-08-02

### Fixed
- **A report card's coach memory is now resolved as of the ACTIVITY's date, not
  the clock.** A read about a run on 2026-07-28 has to cite the relationship as
  it stood then — the streaks, the plan misses, the trailing card aggregate that
  were true when he finished it — not figures that have moved in the weeks
  since. Reading today's ledger onto an old card is the same category of error
  as grading it against today's plan, which `build_card` already refuses to do.
  `tools.workout_report_card` passes `today=<activity date>` into
  `memory.render_memory_for_prompt`, mirroring the plan-coach call beside it,
  which has anchored to `target_date` since it was written.

  The cache consequence is a consequence, not the reason. The coach's memory is
  inside the prompt the read cache keys on, and the ledger renders a step-streak
  counter that increments every day, so every stored card's key rotated
  overnight and re-rendering any past card paid a full ~10s SDK call. Measured
  on the live corpus 2026-08-02: **14 of 15 stored cards missed the fast path**;
  only the card rendered that same day hit.

- **`memory.render_memory_for_prompt`'s `today` now anchors both layers.** The
  ledger honoured it; the journal was an unbounded latest-N list beside it, so
  the block described two different moments at once and *any* new entry —
  another card's reflection, the morning brief's — rewrote the memory of every
  past artifact. `journal.list_entries` takes an `on_or_before` bound (off by
  default; every other caller keeps the live view).

- **Stored reads written before 0.40.0 are repaired on open.** That release
  renamed the read's 4th section `load` → `stimulus`, so 12 of 15 live rows
  failed `card_store.read_is_complete` and could never be reused however well
  their key matched. `card_store.migrate_read_section_names` (run from
  `db.init_schema`) renames the key in place. Idempotent, and it touches
  `card_json` and nothing else — `graded_at`, `read_cache_key` and every grade
  column stay exactly as the render that produced them wrote them. A stored card
  is a historical record; this repairs a field name the schema moved underneath
  it, and regrades nothing.

### Added
- **The report-card path is in the perf-benchmark gate.** It was not benchmarked
  at all. `test_workout_report_card_opens_two_connections` pins the whole
  handler at exactly two `db.connect()` opens (one shared by every read, plus
  `save_card`'s own — it runs on a worker thread and cannot share), and
  `test_load_report_card_inputs_opens_no_connection` pins that the inputs load
  never opens one of its own. Plus a latency benchmark on the deterministic half
  (inputs + `build_card`), guarded by assertions so it can't silently start
  measuring a degenerate n/a card. The PDF path is deliberately excluded —
  WeasyPrint/matplotlib latency is font- and machine-dependent and would
  false-fail a 15% floor.
- `scripts/perf_fixture.py` gains `build_report_card_fixture_db`: a separate,
  smaller DB with paced runs *and* paced walking-desk sessions logged as
  `treadmill_running`, per-lap splits with one slow lap, HR zones, and a plan
  prescribing `target_hr_max` — so the benchmark exercises the locomotion
  filter, all three documented split exceptions and the HR-cap path instead of
  their abstain branches. Separate rather than folded into the shared fixture
  because adding paces there moved `get_training_plan_status` +7.2% and
  `get_training_plan_progress` +4.4% against a 15%-of-min gate whose baseline
  may only be recaptured on ubuntu CI.

## [0.41.0] - 2026-08-02

### Fixed
- **The report card PDF was two pages.** `img.split-chart` was capped by WIDTH
  only, so `chart_h_pt` — the knob the density ladder exists to turn — was
  never read by the report-card stylesheet at all and no rung could buy
  vertical room. Measured over the live DB, **3 of 15 stored cards rendered 2
  pages**, with the HR chart landing alone on page 2 under nine inches of
  white. `img.chart` (the brief's) has carried the height cap since
  2026-07-22 and documents exactly this failure mode in a comment; the lesson
  was never applied to the card. Now 15/15 fit one page.

  The card also gets its own `CARD_DENSITY_PRESETS` — a tighter `dense` cap
  (68pt, measured: 80pt still spilled, 78pt was the first value to fit, 68
  leaves 10pt of margin) plus a 4th `ultra` rung. Both are kept OFF the
  brief's ladder deliberately: the brief reads `page_count > 1` to decide
  whether to drop a takeaway, so an extra rung there silently changes which
  takeaways print. The 4th rung also fixes a 14-split half marathon that was
  2 pages on dev for a second, never-diagnosed reason — row count, not chart
  height.

### Changed
- **The Expected column states the bound the grade was measured against.**
  Direction gating means a run on the free side of a one-sided expectation
  scores an exact 0.0 deviation, which is mechanically an A+ — across the 15
  stored cards, 9 of 15 pace deviations were exactly 0.0 and **A+ was 29 of the
  32 A-band grades (91%)**. The grades are right; the display was not. An easy
  day printed

  | Metric | Actual | Expected | Delta | Grade |
  |---|---|---|---|---|
  | Pace | 9:44/mi | 9:39/mi | 5s/mi slower | A+ |

  — a stated target, a stated 5s/mi miss, and an A+, which reads as a
  participation trophy. `pace_deviation` gates easy/long to the FAST side only,
  so 9:39 is a floor, not a point target. It now reads `≥ 9:39/mi` and the A+
  is self-evident. Same for quality pace (`≤`) and rolling-reference distance
  (`≥`). Two-sided expectations — a plan distance, a steady-day pace — keep a
  bare number, because they genuinely are point targets.

- **One Delta grammar: `{magnitude in the row's own unit} {direction}`, never a
  percentage.** The card printed four dialects in four rows — `on target` /
  `5s/mi slower` / `53% over` / `even` — where the percentages were a
  percentage of a distance, of a ratio, and of a percentage. Three different
  quantities wearing one symbol. Distance now reads `0.35 mi long`, HR
  `8 bpm over`, continuity `0.15x over`; pace was already unit-native and is
  unchanged. Continuity's expected bound also moves from ASCII `<=` to the
  typographic `≤` that HR uses in the same column.

- **Dead split-table columns are dropped.** 13 of 15 stored cards had an
  entirely empty Elev column, and 362 of 428 `activity_splits` rows (85%) carry
  no elevation — a treadmill run never has any. A column now renders only if
  some row has a value for it, and `Avg HR`/`vs run` drop together when the
  watch recorded no per-split HR. `vs run` is deliberately KEPT when it has
  data. Headers and cells come from one shared `report_card.split_table`, so
  the markdown card and the PDF cannot disagree about which columns exist —
  the same reason `stimulus_rows` is shared.

## [0.40.2] - 2026-08-02

### Fixed
- **A prescribed HR cap is graded on how far over it you went, not on how long.**
  0.40.0 measured a cap breach as the *fraction of the run above the ceiling*,
  subtracted a 5% grace and fed the result into `GRADE_BANDS` — a table
  calibrated for relative magnitudes, where 0.53 means catastrophic. A time
  fraction is not a relative magnitude, so this was a category error, and taking
  `max()` of it against the average-over-cap compared two different units.

  It counted a split as *entirely* above the cap whenever its average exceeded
  the cap by any amount. On the live 2026-08-02 card — easy 5 mi, "keep HR under
  140", executed at **139 average with a 148 peak and zero seconds in Garmin
  zones 4-5** — three miles averaged 141, 143 and 142. One to three bpm over.
  That produced 58% "in breach", a 0.53 deviation, and an **F**, which then
  F-capped a 3.60 GPA down to an overall **C** beside A+ on distance, pace and
  continuity.

  Measured across all 19 completed capped days in the active plan, the time
  axis used **2 of 5 bands** — only A and F, never a letter in between, 9 of 19
  failing — and it ranked the mildest breach in the window (1% of it in zones
  4-5) as the single worst session of the nine it failed. Widened to the
  trailing 90 days (43 runs with HR-carrying splits, cap 140): **F 32 (74%), A
  7, D 4, B and C empty**, with 7 runs failed despite an average at or under the
  cap. The corrected axis grades that same population A 15 / B 3 / C 8 / D 4 /
  F 13 — 5 of 5 bands — and fails **zero** runs whose average obeyed the cap.
  The `hr_cap_deviation` docstring's claim that "20% over is a C" was an
  unvalidated assertion introduced by the same commit as the axis; no run in the
  window ever landed in that band. It is gone.

  The graded quantity is now `hr_exceedance_bpm`: the time-weighted mean bpm
  *above* the ceiling, put through the raw-excess-past-a-noise-floor treatment
  `continuity_deviation` already uses (`HR_CAP_NOISE_BPM` 1.5,
  `HR_CAP_BPM_SCALE` 28.0). Both cap axes now speak bpm-over-cap, so the `max()`
  between them means something. Genuine breaches are untouched: 2026-07-22
  (average 157, splits reaching 185, 48% in zones 4-5, 19.5 bpm over) stays an F
  and still caps the overall. Six of the nine F's regrade — to A+, A+, C+, C, C-
  and D — and the three that remain are exactly the three whose zone-4+5 share
  reached 42%, a signal the grade does not read.
- **The average-over-cap axis is no longer divided by the cap either.** Same
  compression: HR's large non-zero offset put a run averaging 168 against a
  prescribed 140 — 28 bpm over — at a **C**. It is an F.
- The HR row's display contract from 0.40.1 carries onto the new axis, and the
  three cells now reconcile by arithmetic (`actual - expected = delta`). A run
  that drifted over the cap by less than the noise floor keeps its bpm display
  and reads "in range", with a note that reconciles the passing grade against
  the time fraction rather than stating the fraction alone beside an A+.

## [0.40.1] - 2026-08-02

### Fixed
- **The HR row now states the axis that produced its grade.** A prescribed cap
  is breached two ways — *average* over the ceiling, and *time* over it past the
  5% grace fraction — and the grade takes the worse. The row displayed only the
  average, so the live card for 2026-08-02 printed:

  | Metric | Actual | Expected | Delta | Grade |
  |---|---|---|---|---|
  | Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |

  Every number there describes the average, which was **under** the cap and
  scored 0.0. The F came entirely from 58% of the run sitting above 140 — a
  quantity the row never showed. Three passing numbers beside a failing letter
  read as a broken grade; the grade was right and the row was lying about why.
  It now reads `| 58% above cap (avg 139 bpm) | ≤ 5% above cap | 53% over | F |`.

  New `hr_cap_axis()` names the governing breach (`"time"`/`"average"`/`None`)
  and shares one helper with `hr_cap_deviation()`, so the letter and the row
  explaining it cannot be computed from different formulas. When the *average*
  is what breached, the row stays in bpm exactly as before.

  **No grade changes** — deviations, letters and GPAs are byte-identical; this
  is display only. The numeric `actual`/`expected` fields stay in bpm, so stored
  cards, the note line and the coach read are untouched. The markdown and PDF
  surfaces share the three display helpers, so both are fixed.

## [0.40.0] - 2026-07-29

The report-card rubric was **inverted**, and this splits it in two. Found by
grading two real sessions from the same week against the same prescription
("Easy 5mi. Keep HR under 140.") and comparing the letters.

### Changed
- **Compliance and stimulus are now separate surfaces on the card.** The graded
  letter answers "did you execute the prescription?" (distance, pace, HR). A new
  **Stimulus** section reports training load, aerobic/anaerobic TE, HR-zone
  distribution and drift with a `LOW|MODERATE|HIGH|VERY HIGH` descriptor and
  **no letter at all**.

  **Why:** Garmin's training load is essentially `duration × f(HR)`, so grading
  load *and* HR graded one variable twice with the sign reversed — obeying an
  easy day's HR cap mechanically drove the load number down, and load's
  undershoot penalty then punished exactly the compliance the HR grade had just
  rewarded. Measured against the live DB on 2026-07-29 (`median_hr` 143,
  `median_load` 99.7 over 23 comparable treadmill runs):

  | Session | dist | pace | HR | load | GPA | Overall |
  |---|---|---|---|---|---|---|
  | 5.01mi, HR 126, even splits — prescription followed exactly | A+ | A+ | A+ | **F** | 3.60 | **C** |
  | 5.00mi, HR 144, cap blown from mile 3 (splits 150/144/159) | A+ | A+ | A− | A+ | 4.00 | **A** |

  The obedient run scored C; the disobedient one scored A. A compliant sub-cap
  50-minute run tops out near 70 load and a *properly* easy one lands near 25,
  so load's F threshold sat **above the physical maximum of a compliant easy
  run** — the grade was unreachable, not merely strict. Load is now absent from
  every `INTENT_METRIC_WEIGHTS` table, which makes "load cannot lower your
  grade" a property of the data structure rather than of a small weight.
- **The F-cap is kept, its scope narrowed.** A card printing "Overall: A" above
  an F row is still averaging away a finding, so the cap stays; `overall_grade`
  now tests only the *weighted* metrics, so a stimulus row can never fire it.
  The cap was never the wrong rule — load was the wrong thing to apply it to.
- **`LOAD_FACTORS["easy"]` 0.75 → 0.61**, measured: the 9 sessions at or under
  the easy HR ceiling in the live 60-day pool median 60.5 load against the
  pool's 99.7. Descriptor-only now, so it can be recalibrated freely. Recorded
  because it is instructive: recalibrating *alone* would not have fixed the
  card (a 25-load easy run still deviates 0.58 against a 61 expectation — an F).
- **`reference_line` no longer blames the reference pool for metrics that don't
  use it.** The scoped "…ungraded — only 2 comparable activities in the last 60
  days" caveat now lists only metrics whose reference IS the pool. Continuity
  measures a run against its own splits and a by-feel pace has no target at all,
  so naming either promised a grade more history could not unlock.
- **Card sections renamed**: the graded table is "Compliance"; the coach read's
  4th section is `STIMULUS` (was `TRAINING LOAD`). Same arity, so
  `read_is_complete` and the prompt contract keep their shape — but
  `read_cache_key` changes, so stored reads regenerate once on next view. That
  is intended: an old read argues about a load *letter* the card no longer prints.

### Added
- **`plan_workouts.target_hr_max`** and `update_plan_workout`'s `hr_max`
  parameter — a prescribed HR ceiling is now a **column**. Before this, "Keep HR
  under 140" lived only in the prose `description` and *no grade could read it*;
  HR was measured against `0.97 × rolling median`, which happened to equal 139,
  so blowing a prescribed 140 cap by 5 bpm cost a single +/− modifier. Added via
  a guarded `ALTER` (the `activities.source` pattern), so existing DBs migrate
  in place. Bounded to 90–210 bpm, and cleared on a `type='rest'` flip.
- **HR is graded on time-above-cap, not just the average** — the worse of
  *average over the cap* and *fraction of split time over it past
  `HR_CAP_GRACE_FRACTION` (5%)*. The average alone is what let a run spending
  miles 3–5 at 150/144/159 read A−. With the cap present, the same three
  sessions now grade **A / C / B** where they previously graded **C / A / C**.
- **`continuity`, a 4th compliance metric** — slowest full split / median full
  split, penalized past `CONTINUITY_TOLERANCE` (1.15) as the raw excess. It
  answers the one question distance, pace and HR all average away: *was this one
  continuous session, or did it contain a break?* Measured 2026-07-28: a tempo
  day whose 4th mile ran **12:31 among ~9:20 miles** graded A+ on distance, A+ on
  HR, and nothing on the card mentioned it. It now grades C+ and the note names
  the mile.

  Two design choices worth recording, both aimed at not inventing a new
  unfairness while fixing the old one:
  - **Not a standard deviation.** The opening mile is routinely the outlier —
    2026-07-27 measured SD 22.2 s/mi across the run and 4.9 s/mi once the warm-up
    is dropped. A metric that failed a run for starting conservatively would
    recreate exactly what the compliance/stimulus split just removed. The
    slowest-vs-own-median ratio leaves that session at 1.08, comfortably inside
    the gate.
  - **Not absolute walk-pace detection.** 12:31/mi is *under*
    `RUN_PACE_CEILING_SEC_PER_MI` (13:00), so the existing run/walk boundary
    would have missed the very session this metric exists for.

  Verified independent rather than redundant before shipping: three sessions in
  the live 90-day window pass distance, pace **and** HR while carrying a slowest
  split 30-41% off their own median. Threshold 1.15 separates cleanly — 33 of 40
  split-bearing sessions sit at or under it, and all 7 above are genuine run/walk
  sessions. Requires 3 full paced splits; below that it is n/a with the reason
  stated and its weight redistributes, which also excludes manually-lapped
  interval sessions by construction. Stored in the new
  `report_cards.continuity_grade` column.
- **HR-zone aerobic share** on the card (`zone_summary`), reading
  `activity_hr_zones` — populated for 90 of the last 90 days. It is the number
  that makes "this really was easy" checkable: the two easy days above share a
  prescription but split **97% vs 30%** aerobic.

## [0.39.0] - 2026-07-27

Evidence-driven audit across accuracy, persona, UX and speed. Every item
below was found by measuring against the real live database, not by
reading code — the "why it mattered" numbers are live observations.

### Fixed
- **Body Battery has been dead since January.** `_ingest_day` read
  `min`/`max` keys Garmin does not return, so both columns were NULL on
  all 2,156 rows while `charged`/`drained` landed fine. They are now
  derived from the per-minute `bodyBatteryValuesArray` the same payload
  already yields (no extra API call), with
  `recompute_body_battery_minmax()` + `fitness recompute-body-battery`
  to repair history from the 13,603 stored samples.
- **The morning brief compared partial-day metrics against full-day
  baselines.** A 06:30 brief printed `Avg Stress 17 vs 32 baseline,
  -47%` and narrated it three times as recovery evidence — the 17 came
  from 50 stress samples covering 00:00–02:27. Cumulative metrics
  (`PARTIAL_DAY_METRICS`) now anchor derived comparisons on the last
  complete day; raw point-in-time reads keep today's value but are
  flagged. Point-in-time metrics (rhr, sleep, vo2_max) are untouched.
- **TSB pre-credited today as a completed rest day.** At 06:30 with no
  activity logged, form read `-12.74` ("fatigued") against yesterday's
  completed `-22.41` ("very fatigued") — a full zone flip that inverted
  again the moment a run synced. `current form` is now the last complete
  day (`current_form_date`), with today's value preserved and clearly
  labelled `projected_end_of_day`.
- **A walking session could score an A against a prescribed run.** A
  4.00 mi walk at 14:09/mi took pace `A+`/overall `A` against an
  "Easy 4mi @ 9:39/mi" prescription, and a 2.42 mi walking-desk session
  at 83:49/mi graded overall `A`. Three compounding causes: the easy/long
  pace arm had no slow-side floor, `build_card`'s plan branch never
  applied the 0.27.0 measured-locomotion rule, and `_select_activity`
  took the day's *first* session (a walk on 26 of 52 multi-activity
  days). Walk-effort activities taking an A-band pace grade on a
  plan-prescribed running day went 8/10 → 0/10. Plan adherence and
  `_foot_distance` are deliberately unchanged — walking still counts.
- **The coach quoted receipts that were false.** `notable_results`
  promoted any `done` quality day to a quotable receipt without
  consulting its graded card, so "Jul 21: interval day done as
  prescribed" shipped into every voice surface for a session graded
  **D** — directly above the journal entry correcting it. Overachievement
  was also measured on on-foot distance, so a day whose run matched its
  target to within a metre (plus a 17:06/mi walk) printed "ran past its
  target", and a pure-walk day with zero running would too. Both now
  gate on graded results and run distance.
- **Journal entries carried letter grades and CTL numbers** into
  `workout_coach`'s prompt — the exact surface 0.28.1 scrubbed after
  measuring leakage. `parse_reflection` now enforces deterministically
  (signed grades, `graded X`, slash-runs like `(A/A+/A+)`, CTL/ATL/TSB)
  and strips a model-written leading date.
- **The compact memory block shipped a dangling empty header.** The
  600-char cap evicted every journal line but left
  "Your journal (what you wrote down):" standing — an invitation to
  invent. Ledger and journal now budget separately.
- **`recovery_pattern` was a dead tool.** A missing body-battery
  baseline discarded the whole workout, so every window inside six
  months returned 0 matched while the rhr baseline was current. Now
  gated per channel, with a stated reason when a channel is n/a.
- **Sample timestamps were host-timezone-dependent.** `_ingest_day`
  converted epoch-ms with no `tz=`, so the stored wall-clock inherited
  the ingesting process's zone — a measured +5h split across 49 days.
  Conversion now uses each payload entry's own local/GMT delta.
- **The grounding monitor could not detect a sign inversion.** Matching
  on `abs()` meant a cited `+22.4` against a real `-22.4` — "rested, go
  hard" vs "very fatigued, back off" — scored clean, and the reported
  delta was wrong by ~2x. It now matches signed values with a distinct
  `sign` flag class, pools raw values rather than tokens scraped from
  display strings, matches within-unit, and includes `workouts_14d` /
  `plan_today` / `anomalies` in the pool.

### Added
- `effort` (`"run"`/`"walk"`/`null`) on workout payloads, measured by
  pace via `interpret.is_running_effort` — Garmin labels walking-desk
  sessions `treadmill_running`, and 28 of the last 61 activities
  labelled as runs were walks.
- `get_training_plan_draft` — a draft plan had no read tool at all, so a
  real 59-workout draft could not be listed, committed or discarded
  (both lifecycle tools require a `plan_id` nothing surfaced).
- `projected_end_of_day` on the training-load payloads.

### Changed
- Payload floats round at the `_text()` boundary (2dp, 4dp for pace and
  training-effect) — `"distance_meters": 6436.27978515625` became
  `6436.28`, ~10% smaller on the highest-traffic tool.
- The coach's memory doctrine moved into `prompts.coach_memory_block`,
  which takes no profile or spec argument — a tuned personality spec
  replaces the profile wholesale, which had silently deleted it from
  every surface.
- `matplotlib` pre-warms on `mcp-stdio` start (~200 ms off the first
  chart of a session).

### Notes
- A historical repair for the timezone drift was written and
  **withdrawn**: rebuilding samples from `raw_json` destroyed 699 of
  13,603 rows (that payload holds only the last pull, while the table
  accumulates the union across pulls). The forward fix ships; repairing
  the ~18,000 already-misfiled rows needs a design that cannot lose
  data. Stress history is additionally unrepairable — `raw_json` never
  carried the stress payload.

## [0.38.1] - 2026-07-26

Live-session polish: two warts caught while grading a real run on the
freshly shipped code.

### Fixed
- `goal_gap` rounds at the payload boundary (a live payload shipped
  `gap_seconds: 186.44919632676692`) and carries a signed `gap_formatted`
  duration (`"+3:06"`) for the coach voice.
- The report card's distance delta no longer prints `-0%` for a
  within-rounding-of-target distance (4.00-of-4.00 mi, short by meters) —
  it says `on target`, matching pace's existing behavior.

## [0.38.0] - 2026-07-26

Maintainability pass (batch 4 of 4 from the 2026-07-26 audit): the lint
gate becomes real, the docs become tested surface, and the one structural
inversion that forced seven lazy imports is undone. No behavior changes.

### Changed
- **`READ_SECTIONS` moved to `report_card`** — its true home (it is the
  card's contract; `render_markdown`, `visuals` and `card_store` all
  consume it). Housing it in `workout_coach` forced every `report_card`
  reach in that module into a lazy import; the move deletes seven of them
  plus four pure-indirection wrapper functions. `workout_coach` re-exports
  the name, so importers are unaffected; a subprocess test pins that
  `report_card` now imports without `workout_coach`.
- **Ruff runs an explicit ruleset** (`E4,E7,E9,F,I,UP,B,A`) — the repo
  carried `noqa: BLE001` comments for a rule that was never enabled (ruff
  ran defaults-only, so the blind-except gate reviewers assumed existed
  didn't). ~75 violations fixed: import sorting, pyupgrade modernization,
  `zip(..., strict=True)` on same-length series (length drift now errors
  instead of silently clipping a chart), a `raise ... from e`, a frozen
  `GradingConfig` default singleton. `BLE`+`RUF100` deferred as a
  documented pair (30 broad-except sites need per-site triage); `ARG`
  deliberately never (MCP handlers take `_args` by contract).
- **Docs are tested surface.** README claimed 37 tools; the truth was 45.
  Eight missing pages written (the whole coach-memory, personality and
  report-card-store surface — including `recall_coach_memories`, the tool
  the coach is told to call before claiming it doesn't remember), and
  `tests/test_docs_drift.py` now fails the build on a missing page, an
  orphan page, or a stale count.
- Six plan-validation tests upgraded from `assert err` truthiness to
  pinned messages (two boundary checks could previously swap without a
  test noticing); dead `plans._foot_duration` deleted; the mile constants
  are `units.METERS_PER_MILE`/`KM_PER_MILE` (public, single source —
  `interpret`'s private copy stays for its stdlib-only contract, pinned
  equal by test); `plan_coach`'s one silent fail-open now logs like its
  siblings; a stale `units.format_hm` docstring stopped claiming the
  dependency points the wrong way.

## [0.37.0] - 2026-07-26

UX pass (batch 3 of 4 from the 2026-07-26 audit): forgiving inputs, honest
truncation, actionable errors, staleness surfaced.

### Changed
- **`query_workouts` returns an envelope** — `{"workouts", "count",
  "truncated"}` instead of a bare array (breaking for payload-shape
  consumers). "Show me all my runs this year" no longer silently answers
  from a clipped 50: the `limit+1` fetch sets `truncated`. `limit` is
  validated 1–500 (`-1` used to reach SQLite as LIMIT -1 — an unbounded
  table dump into context). `list_report_cards` and `list_coach_memories`
  gained the same `truncated` flag.
- **`update_plan_workout` pace accepts `"M:SS"`** (preferred) or decimal
  minutes, with the trap documented in the schema: a model copying the
  display string "9:39" as the float 9.39 silently prescribed 9:23/mi — a
  16 s/mi error invisible in the echo. Implausible paces (outside
  3:00–30:00/mi) are rejected.
- **One real date validator.** `date.fromisoformat` replaces the shape
  regex that accepted `2026-13-45`/`2026-02-30` (impossible dates matched
  nothing in SQL — unreadable as "bad input" vs "empty window"), unifying
  the two error-message idioms across every dated tool. `compare_periods`
  additionally rejects reversed ranges; `find_anomalies` bounds
  `sd_threshold` to 0.5–10 (an explicit 0 — every day an anomaly — now
  errors instead of silently becoming the default).
- **Errors say what the model can act on.** `run_sql` carries the SQLite
  detail ("no such column: sleep_hours" — the model's own typo, the whole
  correction signal; a deliberate reversal of the earlier don't-leak
  stance, safe under the read-only URI gate). Pydantic failures compact to
  `loc: msg` pairs instead of the multi-line repr with a docs URL. PDF
  render failures return one stable line and put the raw traceback in the
  server log where it belongs.
- **`min_distance_mi`** on `query_workouts`/`recovery_pattern` — this is a
  miles-display app, and "runs over 5 miles" sent as `min_distance_km: 5`
  filtered at 5 km. The km param stays as a deprecated alias (mi wins).
- **The `/coach` snapshot flags stale training load** — CTL/ATL/TSB now
  render with `as of <date>` plus a warning line when the baselines row
  lags (TSB decays daily, so a five-day-old row misstates freshness with
  full confidence), and an all-empty day says "No Garmin data for … yet"
  instead of a silent table of dashes.

### Added
- **`get_metric` attaches the baseline read** (`baseline_60day_mean`,
  `current_vs_baseline_sd`, `vs_baseline`) for baselined metrics, mirroring
  `get_metric_trend` — "is 52 high?" no longer costs a second call.
- `units.from_miles`, `units.parse_pace_min_per_mi`,
  `units.pace_sec_per_mi_to_sec_per_km`; `METERS_PER_MILE`/`KM_PER_MILE`
  are public.
- `training_load_status`'s TSB band description is generated from the
  `interpret` constants (can't drift from the classifier).

## [0.36.0] - 2026-07-26

Speed pass (batch 2 of 4 from the 2026-07-26 audit): fewer connections,
fewer queries, fewer sleeps, shared render caches. No behavior changes —
every payload byte-identical except the two additions noted.

### Changed
- **The HTTP persona is memoized.** Stateless `/mcp/` mode resolved the full
  coach persona (6 DB opens + a complete relationship-ledger compute, ~5× the
  cost of the tool call it wrapped) on **every request**. Now memoized behind
  a `(today, db_path, PRAGMA data_version, notes-file stat)` key on a
  dedicated read-only monitor connection — any DB commit, notes edit, or day
  rollover re-resolves; failures and the fresh-clone no-DB path are never
  cached (fail-open preserved).
- **Sync fast-path.** `daily.pull` no longer re-fetches HR zones/splits (or
  pays their 0.3 s throttle) for past activities whose details are already
  stored — the repeated-detail-call shape that trips Garmin's 429; today's
  activities always re-fetch (still finalizing). The day loop shares one
  connection with per-day commit/rollback, never sleeps after the final day,
  and scans the historical gap once instead of twice.
- **`baselines.recompute` is single-pass.** The per-day AVG scan + duplicate
  SD scan (3 statements/day; a backdated manual workout could issue ~2,200)
  became one fetch + one pure rolling-window walk + one `executemany` — 3
  statements total, bit-identical output (pinned against the retired SQL
  reference in tests). `log_manual_workout`/`delete_manual_workout` now run
  it via `asyncio.to_thread` instead of blocking the event loop.
- **`recovery_pattern` dropped its N+1** (~953 point queries on a year of
  running → 3 range queries, identical thresholds and rounding).
- **One read connection through the card/report/reflect pipelines.**
  `workout_report_card` (was ~8 opens), `generate_brief_report`, `reflect`,
  and `get_coach_personality` (4 opens + two COUNT scans → 1 open + one
  aggregate) each share a single connection for their read phase; writes keep
  their own (worker-thread same-thread rule).
- **Query pruning + a new index.** `SELECT *` no longer drags `raw_json`
  blobs (50 KB/activity row, 16.5 KB/daily row) through the report-card
  activity pick (7.7 ms → ~0.2 ms before indexing), the status metric window,
  or `get_workout_detail`; new `idx_activities_date_start` serves the
  `date DESC, start_time DESC` sorts (applies automatically via
  `init_schema`).
- **Render caches.** `fit_one_page` shares one FontConfiguration + image
  cache across density rungs (was: re-decode ~124 KB of chart base64 + re-parse
  a 140 KB TTF per rung); `plan_coach`'s disk cache is multi-entry (v2 format,
  32 entries — alternating two brief dates no longer thrashes a 30 s SDK call;
  v1 files read as a hit); `coach.load_profile` is lru_cached;
  `card_store.load_read` extracts the read via `json_extract` instead of
  decoding the whole stored card; the V2 brief no longer computes-and-discards
  the full memory render; `import tools` no longer pays garminconnect's 28 ms
  (deferred into `sync_garmin_data`).

### Added
- `recovery_pattern.n_skipped_no_baseline` — workouts that matched the filter
  but had no usable baseline row were silently dropped; now counted.

## [0.35.0] - 2026-07-26

Accuracy pass from the 2026-07-26 three-axis audit: wrong numbers stop
reaching the coach's voice. First of four planned batches (accuracy →
speed → UX → maintainability).

### Fixed
- **`sync_garmin_data` no longer reports a healthy sync as an error, and
  recomputes baselines whenever new data lands.** `daily.pull` returns
  `partial` whenever any gap remains back to 2020-09-01, so a single missing
  historical day made every sync return `is_error: true` *and* skip the
  CTL/ATL/TSB recompute — fresh workouts landed while training load silently
  froze. Only hard failures (`auth_failure`, `not_configured`, `failure`,
  `interrupted`) are errors now; the recompute triggers on
  `days_pulled > 0 or activities_loaded > 0`. The payload gains a
  deterministic one-line `sync_state` read and a countable `days_failed`
  (also added to `daily.pull`'s return dict), and the error payload no longer
  drops `deferred_count`/`gap_days_remaining`.
- **Brief signals judge runs by measured pace, not Garmin's label.**
  `days_since_last_run`, `runs_14d`, `runs_prior_14d` and `recent_te` now
  gate on `interpret.is_running_effort` (on-foot checked first — pace alone
  would promote a fast bike ride), falling back to the activity-type label
  only for paceless rows. Walking-desk sessions file as `treadmill_running`,
  so the planner previously saw a run every day and the brief could never
  say "you haven't actually run in eight days."
- **Report card's reference line can no longer contradict its own grades.**
  A plan-graded card with thin comparable history printed "Not enough
  comparable history to grade" directly under a real overall grade; the
  disclaimer is now scoped to the metrics it actually applies to ("HR and
  training load ungraded — only 2 comparable activities…").
- **The card's "setting up for" block gets its numbers back.**
  `workout_coach._describe_prescription` read display keys that upcoming
  plan rows never carry, silently dropping every distance/pace/duration from
  the prompt. It now converts the stored `target_*` columns. One-time read
  regeneration per card on next render (prompt-hash cache bust).
- **Double days grade the session the prescription was written for.** The
  date-branch of `workout_report_card` picked the day's *last* session while
  grading against the primary (lowest-seq) prescription — a PM shakeout got
  graded against the AM long-run target. It now takes the day's first
  session, and `other_activities_on_date` carries
  `{activity_id, activity_type, distance_mi, start_time}` instead of bare
  ids so the model can offer to grade the other one.
- **Manual-lap interval sessions stop being graded at warmup pace.** The
  quality-day splits exception filtered to "full" splits — defined relative
  to the workout's own longest lap, which on a 2-mile-warmup-then-800s
  session is the warmup. `fastest_rep_split` (formerly
  `fastest_full_split_pace`) now selects by a 300 m distance floor
  (`QUALITY_MIN_SPLIT_M`) so the fastest rep-sized split is graded; with no
  qualifying split it returns n/a with a stated reason, never warmup pace.

### Added
- **`get_brief_context` gained continuity and freshness.** The handler now
  passes the last seven saved briefs in, so `continuity` is populated over
  MCP instead of permanently `[]`, and `BriefContext` carries
  `data_frontier`, `baseline_stale_days`, `brief_stale_days` and `tsb_zone`
  — surfacing the orphaned-sync state that was invisible from the tool
  surface. All four fields are optional and ride the existing single DB
  connection (the ==1-connect perf gate still holds).
- **Riegel projection names its basis.** `predicted_finish_seconds` now
  ships with `projection_basis` ({distance_mi, pace_min_per_mi, date,
  extrapolation_ratio}) and `projection_confidence`
  (`interpret.riegel_confidence`: high ≤1.5×, medium ≤3×, low beyond).
  `best_recent_effort` is pace-gated (a mislabeled walking-pad session can
  no longer be the "best effort") and prefers efforts ≥ goal/4, falling
  back to the 2 km floor — labeled low-confidence — rather than dropping
  the projection.
- **Adherence splits out rest days.** `sessions_adherence_pct` (rest
  excluded) and `rest_days_counted` join the untouched `adherence_pct` in
  plan status/progress payloads, the brief-PDF plan strip, and the
  plan-coach prompt — a week of 3 kept rest days + 4 skipped runs no longer
  headlines as 43% without the 0% session number beside it.
- **`last_graded` says which day.** `_slim_workout` now carries `date` and
  `seq`, so "you missed your long run" can say when.

## [0.34.0] - 2026-07-26

Past report cards become part of the coach's standing memory, without
touching the raw-card-injection rule that protects the plan/workout-coach
prompt-hash caches.

### Added
- **Trailing-3-week report-card aggregate** in the relationship ledger
  (`ledger.report_card_facts`): count of graded workouts, average GPA,
  base-letter grade distribution, and a rising/falling/flat trend
  (`interpret.pct_change` + `delta_direction` over a recent-vs-earlier
  split). Flows into `memory_text` on every voice surface via the existing
  ledger block. Computed ONLY over cards with `activity_date` strictly
  before today — the same as-of-yesterday discipline `step_streak_facts`
  already uses — so grading today's workout never changes today's memory
  and never busts the render that produced it.
- **Standing chat directive**: the coach now calls `list_report_cards` /
  `get_report_card` proactively for grade or trend questions, grounded by
  a rule against stating any letter grade or GPA that didn't come from a
  tool call or the memory section's computed line.

### Changed
- `card_store.py`'s "never inject raw cards" rule now names its one
  sanctioned exception (the deterministic, as-of-yesterday aggregate above)
  without weakening the rule for anything else — see the module docstring
  and `CLAUDE.md`.

### Known residual
- Grading a *prior-day* workout mid-day flips `memory_text` once that day
  (bounded, self-converging — the aggregate is idempotent under re-saves,
  same order of magnitude as an existing intra-day journal write). Not
  fully closed in this release; a `first_graded_at` column is the deferred
  watertight fix if it ever proves annoying in practice.

## [0.33.0] - 2026-07-25

The coach stops forgetting: the journal archives instead of deleting, and a
new recall tool searches everything it has ever written down.

### Added
- **Journal archive** — the 60-entry cap now flips an `archived` flag instead
  of DELETEing. The hot injected set (and every prompt-hash cache keyed on
  it) is unchanged; the journal itself never forgets. Only user-requested
  `delete_coach_memory` removes entries for real.
- **`recall_coach_memories` tool** — keyword search over the WHOLE journal
  (hot + archived) via a new FTS5 external-content index
  (`coach_journal_fts` + sync triggers, porter stemming, BM25 best-first).
  User query tokens are quoted as phrases so MATCH syntax is inert; a LIKE
  fallback keeps recall working on SQLite builds without FTS5 (`search`
  field in the response says which mode answered). Pure JSON — reachable
  over stdio AND the networked `/mcp/` transport. The FTS DDL lives in its
  own `FTS_SCHEMA` script (never in `SCHEMA`) so an FTS5-less build still
  boots; the index self-heals on count mismatch against the `_docsize`
  shadow table.
- **Capture directives** in the coach's standing instructions (the MCP
  instructions payload): save durable facts when shared (injury, schedule
  constraint, goal change, preference), write a 1-2 line session note after
  substantive conversations, and search recall BEFORE claiming not to
  remember — never citing a memory the search didn't return.

### Changed
- `list_coach_memories` gains `include_archived` for browsing past the hot
  60; `get_coach_personality` reports `journal_entries` (hot, as always)
  plus a new `journal_archived` count.
- `LOCAL_FITNESS_COACH_MEMORY=0` still governs injection and auto-reflect
  only — recall reads journal data directly, which survives the kill switch
  (the documented contract: the switch never touches journal data).

## [0.32.0] - 2026-07-23

Report cards become part of the coach's durable memory: every rendered card
persists as a dated snapshot, queryable from every surface — including the
phone, which could never render a card at all.

### Added
- **`report_cards` store** (`agent/card_store.py` + new table): each render
  of `workout_report_card` persists the full card (grades, intent, GPA, the
  coach's verbal read) as a **dated snapshot** — the card as actually shown,
  graded against the plan active at that render. No backfill: history
  accumulates as cards are rendered. Write is fail-silent via ONE atomic
  guarded UPSERT keyed on the read's prompt key (`busy_timeout=5000`,
  awaited off the event loop): an equal-key render is a byte-identical
  no-op, and a template-fallback render never overwrites a real-read row —
  a stored card's words and grades always come from the same render.
- **Two shared query tools** — `list_report_cards` (date/intent-class
  filters, newest run first — "how have my quality days trended" in one
  call) and `get_report_card` (the stored card, coach read, and verbatim
  markdown). Both in `ALL_TOOLS`: pure JSON, so they reach stdio AND the
  networked `/mcp/` transport.
- **Per-activity read reuse**: the stored card doubles as a persistent read
  cache. `workout_coach.read_cache_key` (the factored single key
  definition) lets a re-render of ANY previously rendered card reuse its
  stored read on an exact prompt-key match — alternating between two old
  cards no longer costs ~10s of SDK call each.

## [0.31.0] - 2026-07-23

Second half of the memory-based-personality build: the coach's personality
is now conversationally tunable, and the shipped default is a rewritten
hard-ass — an original "accountability mirror" persona built to spend the
memory 0.30.0 gave it.

### Added
- **Tunable personality spec** (`agent/personality.py`): a bounded,
  DB-stored spec (settings key `coach_personality_spec` — ≤8 KB, identity
  ≤4000 chars, ≤12 items per list, ≤16 intensity topics) carrying identity
  prose, catchphrases, principles, never-do rules, and per-topic intensity
  (`off|low|medium|high|brutal`). The profile `.md` files become first-run
  seeds; once tuned, the spec is what every voice surface speaks
  (`CoachProfile.effective_persona`). Virtual seeding: an untuned clone is
  byte-identical to before.
- **Two tuning tools** — `get_coach_personality` /
  `update_coach_personality` — the agent-owned write path (there is no UI):
  "ease up about the step goal" becomes `set_intensity:
  {step_goal_nagging: low}`, live on the next render, no restart. Dial
  fields write the existing `coach_*` settings keys; `reset: true` returns
  to stock. A spec tuned for a different profile is ignored but retained
  (reported as `base_profile_mismatch`).
- **Kill switch**: `LOCAL_FITNESS_COACH_SPEC=0` ignores a stored spec
  without deleting it.

### Changed
- **`hardass` is the shipped default profile** (`coach.DEFAULT_PROFILE`,
  `config.coach_profile()`), and `hardass.md` is rewritten from 5 bullets
  into a full personality: the accountability mirror — identity, philosophy
  ("motivation is weather, discipline is climate"), signature lines with a
  one-per-brief cadence rule, a "Using your memory" section that spends the
  0.30.0 ledger/journal as receipts ("Third skipped quality day this month.
  Jul 12, Jul 19, today."), and a hard never-do list (never invent a
  number, never mock injury — on a red recovery day the hard call IS the
  rest, never cite a real coach or athlete). Dials/thresholds unchanged
  (9/1/10 · 1.00/1.05), so the V1/V2 harsh-block gates work untouched.
  Opt-out: `fitness config set coach_profile adaptive` (or supportive /
  neutral).

## [0.30.0] - 2026-07-23

The coach remembers. First half of the memory-based-personality build
(tunable personality spec + the new hard-ass default land separately):
every voice surface now carries a two-layer memory, so briefs, report
cards, and chat can make receipts-backed callbacks ("third missed quality
day this month — Jul 12, Jul 19, today") instead of judging each day in
isolation.

### Added
- **Deterministic relationship ledger** (`agent/ledger.py`): plan-adherence
  miss/done streaks and windows (reusing `plans.build_plan_detail`'s
  verdicts — never re-grading), step-goal streaks computed as-of-yesterday
  (today's partial count can't flip the block intra-day), repeat patterns
  in logged observations (mood/energy/soreness thresholds, injury from the
  first log), and notable recent results. Pure stdlib functions over plain
  dicts, per the `interpret.py` rule: Python derives every count the coach
  may quote; the LLM only phrases.
- **Coach journal** (`agent/journal.py`, `coach_journal` table): short
  dated memory lines the coach writes itself. 60-entry cap pruned on every
  write, 240-char lines, and a partial unique index on
  `(source, source_key, seq)` so one reflected event can never double-write.
- **Auto-reflect** (`agent/reflect.py`): after each saved daily brief and
  each first-render report card, a toolless Sonnet-low single-shot call
  (same measured config as `workout_coach`, ~10s) writes 0–2 journal lines
  — or `NONE`, since most days aren't worth remembering. Fail-silent and
  post-persistence: a reflect problem can never cost the brief or the card.
  `journal.has_event` + `exclude_source_key` filtering make it idempotent
  and cache-cascade-proof.
- **Memory on every voice surface**: `prompts.coach_memory_block` (pure,
  carries the never-invent grounding contract) composed by `system_prompt`,
  `brief_v2_system_prompt` (compact variant, hard-capped at 600 chars so V2
  stays the shrunk prompt), `plan_coach.build_prompt`, and
  `workout_coach.build_prompt` — all as passed-in text, preserving the
  prompt-hash disk caches.
- **Three chat tools** — `save_coach_memory` / `list_coach_memories` /
  `delete_coach_memory` — so the live agent can journal mid-conversation
  (an excuse, a promise, an injury flag) and manage memories when asked
  "what do you remember?".
- **Kill switch**: `LOCAL_FITNESS_COACH_MEMORY=0` disables both the
  injection and the reflect writes; journal data is untouched.

## [0.29.0] - 2026-07-23

Fifteen fixes from a multi-agent audit across three axes — terminal UX, MCP
tool quality, and data reliability. Every finding was independently verified
against the code before implementation; six other claims were refuted and
dropped.

### Fixed — data correctness
- **Tempo/interval days are graded on the full done|partial|missed ladder.**
  The duration branch never consulted `done_fraction`, so any running ≥40% of
  a quality day's target graded a full "done" and such days could never grade
  "missed". The ladder now mirrors the distance grading
  (`done_fraction`/`partial_fraction`); easy/long-day and walk-gating
  semantics are untouched.
- **Backfilled activities land on the right local date.** `backfill.py`
  decoded Garmin's local-epoch `startTimeLocal` with host-TZ
  `datetime.fromtimestamp`, applying the timezone offset twice — a 05:30
  activity filed under the previous day. The local epoch is now decoded
  offset-free; the genuinely-GMT `beginTimestamp` fallback keeps host-TZ
  decoding.
- **A day whose Garmin endpoints all fail no longer writes an all-NULL row.**
  Previously the row masked the gap forever and the pull reported "success"
  having saved nothing; now the day stays missing (retried by gap detection),
  lands in `days_failed`, and the pull reports `partial`. The upsert also
  switched to per-column `COALESCE`, so a transient endpoint failure inside
  the freshness window can never overwrite finalized data with NULLs (this
  incidentally stops the daily pull from clobbering backfill's
  `training_status`).
- **Mid-run Garmin auth expiry aborts the pull.** `_safe`'s blanket
  `except Exception` swallowed `GarminConnectAuthenticationError`, making the
  abort-the-rest handler dead code; the pull could run to "success" on an
  expired token. Auth errors now propagate; other per-endpoint errors stay
  best-effort.

### Fixed — MCP accuracy & terminal UX
- **`run_sql` discloses its 500-row cap.** Results beyond 500 rows set
  `truncated: true` with a hint to add `LIMIT`/aggregate, and the cap is
  stated in the tool description — previously a clipped result was
  indistinguishable from a complete one and totals came back confidently
  wrong.
- **`list_observations` is bounded** (default `limit` 100, `truncated` flag),
  mirroring `query_workouts` — the bare call was an unbounded
  `SELECT * FROM observations`.
- **`update_plan_workout` echoes what it wrote.** The confirmation now
  includes the duration it just set (the graded field on quality days, per
  its own description) and the resolved `seq` on double days.
- **`get_metric` / `find_anomalies` payloads are readable.** Formatted
  companions (h:mm durations, min/mi paces, sensible rounding) ride alongside
  the raw fields, and `get_metric` skips NULL calendar-day padding, reporting
  `days_with_data` vs the window instead.
- **Weekly mileage means the same thing on every surface.** The brief PDF's
  plan section now computes on-foot miles like `get_training_plan_progress`'s
  `week_actual_mi` (walking on easy days counts by design), and `plan_chart`'s
  legend says what the bar actually plots.

### Fixed — output reliability
- **PDF content tags now hash the render's inputs, not the PDF bytes.**
  WeasyPrint's PDF serialization is not byte-reproducible — identical HTML
  diverged on ~50% of paired Linux renders — so 0.28.2's bytes-based
  `_content_tag` broke the "identical content reuses one filename" half of
  its own contract at random (and made its CI test a coin flip). Both PDF
  filenames now derive from the logical content (brief/card + chart bytes +
  brand theme + app version) via `_render_tag`; `generate_chart`'s PNG keeps
  byte-hashing since matplotlib's writer is reproducible.
- **`generate_chart` PNGs are content-addressed** (`…-<sha8>.png`), closing
  the same stale-Preview-refocus hole 0.28.2 fixed for PDFs — an intra-day
  re-render after a sync landed fresh bytes on the same path.
- **A report card that overflows one page says so.** `render_report_card_pdf`
  now returns `(bytes, page_count)` like the brief renderer, and
  `workout_report_card` stamps `pages` + logs a warning when the density
  ladder is exhausted, instead of spilling silently.
- **Training load carries its as-of date.** `assemble_status` adds `as_of` +
  `baseline_stale_days` to `training_load`, so a stale baselines row can no
  longer masquerade as current fitness/fatigue/freshness.
- **The plan-coach fallback line is date-aware.** It said "Yesterday…" about
  whatever day was the latest graded one — including today's own run. The
  reference now resolves Today / Yesterday / the actual date.
- **The MCP server logs its coach-persona fail-open** instead of silently
  serving a persona-less server when the profile can't load, and the drifted
  `LOCAL_ONLY_TOOLS` docs on the transport boundary were corrected.

## [0.28.2] - 2026-07-22

### Fixed
- **Re-generated PDFs no longer show a stale render.** `generate_brief_report`
  and `workout_report_card` wrote a deterministic filename (`brief-<date>.pdf`,
  `report-card-<id>.pdf`), so a re-render reused the same path — and macOS
  `open` refocuses an already-open Preview window instead of reloading the
  bytes. The result was a stale-looking page that made a user conclude the data
  pipeline was serving old data (it wasn't). Both filenames are now
  content-addressed (`…-<sha8>.pdf` via `tools._content_tag`): changed content
  lands on a fresh filename (a new window), identical content reuses the file
  (idempotent).

### Changed
- **`save_brief` now advertises the real Brief JSON Schema** instead of an
  opaque `{"brief": dict}`. Derived from the pydantic `Brief` model (with
  `$defs` hoisted to the schema root and `required` narrowed to the caller-
  supplied `takeaways`), so a client can construct a valid brief — including the
  tone enum and `{metric, days}` sub-object — from the tool contract alone,
  rather than reading `schemas.py`. Filesystem-less MCP clients (Claude Desktop,
  a phone over `/mcp/`) can now build a brief they previously could not.

## [0.28.1] - 2026-07-22

### Fixed
- **The report-card read named a letter grade, because the prompt handed it
  every letter.** `_GRADE_TONE` forbids it outright, yet the user prompt
  printed `Distance: D- — actual 5.95 mi vs target 5.00 mi` for each metric and
  `Overall grade D (1.05 GPA)` above them, and `_GRADE_TONE` itself spelled out
  `"A"`, `"B-"`, `"C+"` as examples. The read was being shown the thing it was
  told not to say. Measured over 96 paragraphs on 3 real cards: **3 leaked
  (3.1%)** — "an F, no rounding it up", "F-grade pace", "the C+ says so" — and a
  leaked read **regenerated to the SAME letter**, because the retry saw the same
  prompt.

  The prompt now carries SEVERITY instead (`grade_severity`: on target →
  slightly off → off → well off → missed badly). The severity has to be there or
  the read drifts out of agreement with the table beside it: a +19% distance
  overshoot is a D- only because intent scaling says an interval day is not the
  place for extra miles, and that judgment is not recoverable from the raw
  numbers. Re-measured on the identical protocol: **0 of 96 (0.0%)**, median
  latency unchanged at 9.1s.
- **A backstop for what the prompt can only ask for.** `find_grade_leak` scans
  the parsed read and `generate_read_cached` regenerates ONCE on a hit, keeping
  the retry only if it is actually clean. Deliberately narrow: a bare "A" is
  almost always the article ("A blown interval session…"), and a false positive
  would throw away a clean read and pay for another generation. The pattern is
  specified by two lists in `tests/test_workout_coach.py` — 8 real leaks, 9
  lookalikes — which must both be extended before it is touched. It matters
  more than the rate suggests because reads are cached: a leak would otherwise
  stick until the card's inputs change.

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
