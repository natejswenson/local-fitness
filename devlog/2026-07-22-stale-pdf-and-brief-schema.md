# 2026-07-22 — The stale PDF that wasn't stale, and a wrong diagnosis I had to walk back

## Where this came from

Reviewing a real coaching session (pull data → report card → daily brief →
"open the report" → charts), three friction points surfaced. Two were clean
wins and shipped. One sent me down a wrong path I had to reverse in front of
Nate — which is the more useful story.

## The clean wins

**PDF re-render showed a stale page.** Mid-session Nate said "you create the
report based on old data." The data was current — six DB traces proved it. The
real culprit: `generate_brief_report` wrote a deterministic `brief-<date>.pdf`,
so a re-render reused the same path, and macOS `open` *refocuses* an existing
Preview window instead of reloading the bytes. He was looking at the first
render. A window-management artifact read as a data-integrity failure — and
cost real trust. Fix: content-address the filename (`…-<sha8>.pdf`). Changed
bytes → new filename → fresh window; identical bytes → same file → idempotent.
Same fix on the report-card PDF.

**`save_brief` made the agent read source.** To save a brief the agent had to
grep + Read `schemas.py` for the Takeaway/tone/metric shapes, because the tool
advertised `{"brief": dict}` — an opaque blob. A phone over `/mcp/` couldn't
build a valid brief at all. Fix: advertise the real `Brief.model_json_schema()`
(hoist `$defs` to the root, narrow `required` to `takeaways` since the server
stamps the rest). The SDK only forwards a dict schema verbatim when it has a
top-level string `type` + `properties`, so that condition is now a test.

## The wrong diagnosis

The run Nate flagged: an easy 4mi that "felt good" but scored an F, driven by a
training load of 250. I looked at the aggregates and saw `avg_hr 157 ==
avg_cadence 157`, `max_hr 197`, and confidently diagnosed an optical-HR
**cadence lock** — the wrist sensor reporting steps-per-minute as heart rate. I
told Nate "your gut was right, the watch lied," got sign-off to build a detector
that excludes suspect HR + load from grading and de-contaminates CTL/ATL/TSB,
and wrote the whole thing: a pure `interpret.hr_cadence_locked`, a report-card
exclusion branch, a baselines TSS filter, tests.

Then two facts broke it:

1. My "same-pace comparison" was wrong. The run I cited as the clean baseline
   (HR 112) was at **14:08/mi** — a near-walk, not the same 9:57/mi effort.
2. `157 bpm` is squarely **normal for his easy-pace bucket** (mean 150, up to
   184 across 121 runs), and `HR≈cadence` shows up on 25+ of his treadmill runs
   across years. Not a rare glitch signature at all.

The dispositive check was the per-mile splits. At a dead-flat ~10:00/mi pace,
HR climbed **129 → 144 → 169 → 185**. A cadence lock produces a *flat* line
pinned at cadence; a 56-bpm climb at constant pace is **cardiac drift**. The
`157 = 157` average match was a coincidence of averaging a rising line. The
run genuinely taxed him — exactly what the brief already said (day after a
5.95mi session, freshness −42). The harsh report card was defensible; excluding
its signal would have *hidden* a real "more fatigued than you feel" flag.

So I reverted the detector entirely (`git checkout` on the three files that held
only that code, after confirming the diff) and told Nate directly that the
diagnosis was wrong.

## The lesson

I built on a diagnosis before I'd earned it. `HR == cadence` is a *suggestive*
signature, not proof; the proof lives in the per-sample/per-split shape, and I
should have pulled the splits **before** proposing a detector, not after
writing one. The aggregate coincidence was seductive precisely because it
pointed at the answer I'd already reached for. Cheap to check first, expensive
to check last.

Shipped 0.28.2: the two UX fixes. Dropped the training-load work — the number
was right.
