# 2026-07-23 — Fifteen fixes from a multi-agent audit (0.29.0)

## Where this came from

Nate asked for a systematic improvement pass across three axes: terminal UX
(how answers read in the chat), MCP tool quality (accuracy, efficiency,
cleanness), and data reliability (correct data reaching the user every single
time — terminal and PDFs). Rather than one long read-through, this ran as a
staged multi-agent pipeline:

1. **Find** — six parallel finders, one per dimension, each grounded in actual
   code reads and forbidden from re-litigating designs CLAUDE.md marks
   settled.
2. **Verify** — every medium/high-impact claim got an independent skeptic
   whose only job was to refute it against the real code. 21 findings
   survived; **6 were refuted** (e.g. "backfill clobbers live-pulled rows",
   "pull never recomputes baselines" — both wrong on inspection). That's six
   fixes that would have been wasted or harmful work.
3. **Implement** — three sequential agents (they all touch `tools.py`), one
   commit per fix, each with a regression test verified to fail on the
   unfixed code.

## The ones that mattered

**Quality days couldn't fail.** `plans.py`'s duration branch never consulted
`done_fraction` — any running ≥40% of a tempo/interval target graded a full
"done", and "missed" was unreachable if any running occurred. Adherence
percentages were quietly inflated. The ladder now mirrors distance grading.

**Backfill filed pre-dawn runs on the wrong day.** Garmin's `startTimeLocal`
is a *local* epoch; decoding it with host-TZ `fromtimestamp` applies the
offset twice. A 05:30 activity landed on the previous date — exactly the kind
of silent data corruption the audit existed to catch. The subtle part: the
test fixture had encoded the timestamp the same wrong way, so the round-trip
looked correct. The fixture now encodes the real format.

**A fully-failed pull day wrote an all-NULL row and called it success.** The
row masked the gap from gap-detection forever. Now the day stays missing and
the pull reports `partial`/`days_failed`. Sibling fix: the upsert is
per-column `COALESCE`, so the freshness re-pull can never overwrite finalized
data with NULLs — which incidentally also stops the daily pull from nulling
backfill's `training_status` (a latent bug nobody had filed).

**`run_sql` clipped at 500 rows silently.** The one tool where the model is
doing ad-hoc analysis is the one that most needs to know the data was cut;
totals over the clip came back confidently wrong. It now sets
`truncated: true` with a hint. Same family: `list_observations` was the only
unbounded list on the surface (now `limit` 100 + flag).

**The stale-Preview bug had a third instance.** 0.28.2 content-addressed both
PDF filenames; `generate_chart`'s PNG had the identical intra-day refocus
hole. Same `_content_tag()` fix, same test shape.

## Method notes

- The refutation gate earned its cost: 6/27 verified claims died there, and
  several confirmed ones had their fix *reshaped* by the verifier's notes
  (e.g. run-vs-foot mileage was a labeling/consistency fix, not a gate change
  — walking on easy days counts by design).
- Findings deferred, not dropped: the 4×-copy-pasted plan-loading preamble in
  `tools.py`, extracting the ~640-line PDF subsystem into its own module,
  unifying the three spellings of mile/pace conversion constants, and 15
  unverified low-impact items — all listed in the PR.
- Suite after: 1513 passed, coverage 94.7% (gate 85), ruff clean.
