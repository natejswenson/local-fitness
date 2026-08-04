# Report card: continuous 1-5 star rating (0.50.0)

Design record for the change that replaced letter grades with a continuous
star score. Two specs, written independently against the same measured data,
plus the calibration output the release was gated on.

The scoring half was simulated over **240 real cards spanning 730 days**; the
presentation half was measured by rendering **all 16 stored cards** through the
real WeasyPrint path. Every number below is measured, not modelled.

---

# Report card: continuous 1–5 star scoring

Design spec. Replaces letter grades (`A+`…`F`) and the GPA with a continuous
star score per compliance metric and one for the card overall.

Everything below is measured on **240 real cards / 236 with a usable reference**,
regraded through the production path (`load_report_card_inputs` → `build_card`)
over a trailing 730 days.

---

## 0. TL;DR of the decisions

| # | Decision | Value |
|---|---|---|
| 1 | Functional form | ONE piecewise-linear curve over normalized `z = d_eff / (widen × STAR_SCALE[metric])`, knots derived from `GRADE_BANDS` |
| 2 | `STAR_SCALE` | `0.35` for all four metrics — **no calibrated boundary moves** |
| 3 | Noise floor | `d_eff = max(0, d − STAR_NOISE[m])`; distance `0.0075`, pace `0.002`, hr `0.0`, continuity `0.0` |
| 4 | Floor / max | `STAR_FLOOR = 1.0`, `STAR_MAX = 5.0`, saturating both ends |
| 5 | Internal precision | continuous float, stored at 3 dp |
| 6 | Display quantization | quarter star, **half-up**, applied at render only |
| 7 | F-cap replacement | `overall = min(weighted_mean, worst_row + 2.0)` |
| 8 | Overall | weighted mean of *unquantized* per-metric stars, `INTENT_METRIC_WEIGHTS`, n/a redistributes |
| 9 | HR populations | one scale; the 11.3 bpm F-floor is preserved **bit-exactly**, no zone-4+5 revalidation needed |
| 10 | Stored cards | not re-scored, not recomputed on read — warm them |

Headline result: **overall lands on exactly 5.0 on 17% of cards** (today: GPA
exactly 4.00 on **33%**), spread across **12 of 17** quarter-star buckets
(today: 4 letters, of which 2 hold 86% of the mass). Pace goes from 5 letters to
**15 distinct quarter-star levels**.

---

## 1. THE CORE MAPPING

### Architecture

```python
STAR_MAX   = 5.0
STAR_FLOOR = 1.0

# The deviation knots the whole rubric has always been calibrated on. This is
# GRADE_BANDS with the letters removed — same numbers, same lineage, and the
# last knot is still where a metric bottoms out.
STAR_KNOTS: tuple[float, ...] = (0.05, 0.10, 0.20, 0.35)

# Knots normalized by the last one, paired with the star at each knot. Derived,
# never hand-written, so a change to STAR_KNOTS can't leave the anchors stale.
STAR_ANCHORS: tuple[tuple[float, float], ...] = ((0.0, STAR_MAX),) + tuple(
    (k / STAR_KNOTS[-1], STAR_MAX - 1 - i) for i, k in enumerate(STAR_KNOTS)
)
# == ((0, 5.0), (1/7, 4.0), (2/7, 3.0), (4/7, 2.0), (1.0, 1.0))

STAR_SCALE = {"distance": 0.35, "pace": 0.35, "hr": 0.35, "continuity": 0.35}
STAR_NOISE = {"distance": 0.0075, "pace": 0.002, "hr": 0.0, "continuity": 0.0}


def stars_from_deviation(d, metric, widen=1.0):
    """Relative deviation → stars in [1.0, 5.0]. None in, None out."""
    if d is None:
        return None
    z = max(0.0, float(d) - STAR_NOISE[metric]) / (widen * STAR_SCALE[metric])
    if z <= 0:   return STAR_MAX
    if z >= 1.0: return STAR_FLOOR
    for (z0, s0), (z1, s1) in zip(STAR_ANCHORS, STAR_ANCHORS[1:]):
        if z <= z1:
            return s0 + (s1 - s0) * (z - z0) / (z1 - z0)
    return STAR_FLOOR
```

This is the architecture the brief proposed — one shared monotone curve over a
normalized `z`, one grader — and I am taking it. `grade_from_deviation`,
`_modifier`, `base_letter`, `GRADE_POINTS`, `_GPA_CUTS`, `_F_FLOOR_GRADE` are all
deleted. `GRADE_BANDS` survives numerically as `STAR_KNOTS`.

### Why piecewise-linear on GRADE_BANDS' own knots, and not something smoother

**1. It is the only shape that moves no calibrated boundary.** Every knot is a
boundary the module already ships and that `scripts/calibrate_report_card.py`
already gates. `d = 0.35 × widen` was the F floor; it is now exactly 1.0 star.
`HR_CAP_BPM_SCALE = 28.0` is calibrated so that F begins at 11.3 bpm sustained
over a prescribed cap, validated against Garmin's zone-4+5 share — a signal the
grade does not read. Under this curve:

```
  0.0 bpm over cap -> d=0.0000 -> 5.00 stars
  1.2 bpm over cap -> d=0.0000 -> 5.00        (the 2026-08-02 obedient run)
  1.5 bpm over cap -> d=0.0000 -> 5.00        (HR_CAP_NOISE_BPM, exactly)
  3.0 bpm over cap -> d=0.0536 -> 3.93
  4.55 bpm over cap-> d=0.1089 -> 2.91        (smallest genuine breach in the window)
  8.0 bpm over cap -> d=0.2321 -> 1.79
 11.3 bpm over cap -> d=0.3500 -> 1.00        <-- the validated F floor, exact
 13.6 bpm over cap -> d=0.4321 -> 1.00
 19.5 bpm over cap -> d=0.6429 -> 1.00
```

The three sessions at ≥ 11.3 bpm (11.9 / 13.6 / 19.5 — exactly the three at ≥42%
zone-4+5) all land on the floor. **CLAUDE.md's mandatory re-run of the zone-4+5
comparison is not triggered, because no constant it names has moved.** Any
alternative shape (exponential, logistic, per-metric scale) *would* trigger it,
for granularity gains I measured and found to be nil (§1.4).

**2. The knot spacing is not arbitrary — it encodes a real perceptual fact.**
Band widths double (0.05 / 0.05 / 0.10 / 0.15). The difference between a 2% and a
7% miss matters more to a runner than the difference between 32% and 37%.
Linear-in-`d` would spend half the scale on the far tail.

**3. It defeats the "distance is a flat plateau, so decay is wrong" objection
without decaying.** The concern is that a decaying curve would compress the flat
0.02→0.40 plateau into the top of the scale. This curve is *piecewise linear
with widening pieces* — over the plateau it is close to linear-in-`d`, so the
plateau spreads. Measured: distance occupies **17 of 17** quarter-star buckets
and its 89 nonzero rows spread 15 / 4 / 7 / 10 / 5 / 5 / 4 / 7 / 4 / 1 / 5 / 3 /
4 / 4 / 5 / 6 across buckets 1.00→4.75. That is the plateau surviving the
transform.

**4. Pace only lives in [0, 0.20], so "two-thirds of GRADE_BANDS is decoration
for pace" — and yet pace comes out the best-resolved metric.** Because `widen`
does the work the brief expected `STAR_SCALE` to do. 111 of 236 pace rows are
steady-class at `widen = 1.5`, whose effective range is `d ∈ [0, 0.525]`; their
median `d = 0.0728` lands at 3.54 stars and the tail reaches 1.57. Measured pace
distribution: **15 of 17 buckets, only 24% at 5.0, mean 3.99, median 4.27.**
Pace does not need a rescale.

### The `widen` question (incommensurability flag #1) — KEPT, in the normalizer

`widen` stays inside `z`. An identical 5%-short run earns **different** stars
depending on the yardstick, and it should:

| `d = 0.05` | widen | stars |
|---|---|---|
| plan target (`PLAN_TIGHTEN`) | 0.6 | **3.33** |
| rolling median | 1.0 | **4.00** |
| steady-class rolling pace (`STEADY_WIDEN`) | 1.5 | **4.33** |

`widen` is not a fudge factor, it is a statement about the *precision of the
yardstick*, and the two metrics are answering different questions. Against a plan
the question is "did you follow the instruction" and 5% off an instruction is a
real miss. Against a 60-day rolling median — an estimate with its own spread — 5%
is inside the noise of the reference itself. `PLAN_TIGHTEN = 0.6` exists because
without it a prescribed 10:28 easy run executed at 9:28 scored a B- and let the
card print an overall A above its own read saying "you never ran easy at all".
Collapsing `widen` re-opens that.

**The obligation this creates:** the star row must print its reference inline,
not in a footer. `reference` is already on every metric dict and
`reference_line()` already renders it; the requirement is that it sits *adjacent*
to the stars. Letters hid this asymmetry behind band coarseness (all three of the
above could read "B"); stars expose it, which is honest but only if the yardstick
is legible. See §11.7.

### The noise floor (`STAR_NOISE`) — load-bearing, not polish

Distance is two-sided against a plan and therefore **has zero exact zeros**
(n=17). Its measured values:

```
0.0002 0.0002 0.0004 0.0005 0.0007 0.0011 0.0012 0.0013 0.0013 0.0016
0.0017 0.0019 0.0040 0.0089 0.0189 | 0.1899 0.2098
```

13 of 17 are GPS wobble / treadmill rounding, then a clean gap to 0.19. Without a
noise floor, `d = 0.0089` → 4.70 stars → **quarter-rounds to 4.75**, on the same
row where `_delta_text` prints `on target` (because
`_DISTANCE_ON_TARGET_MI = 0.02 mi` says the gap is rounding). A card reading
`| Distance | 5.01 mi | 5.00 mi | on target | ★★★★¾ |` is precisely the
self-contradicting row the module's display contracts exist to prevent.

`STAR_NOISE["distance"] = 0.0075` (0.75%) is `_DISTANCE_ON_TARGET_MI` expressed
relatively: 0.02 mi on any target ≥ 2.67 mi. It moves the distance floor from
`d = 0.3500` to `d = 0.3575` — a 2.1% loosening of a constant with **no external
validation** (`GRADE_BANDS`' last knot is a generic table, unlike
`HR_CAP_BPM_SCALE`). Measured effect on the whole corpus: distance at 5.0 goes
58% → 62%, everything else unchanged.

`STAR_NOISE["pace"] = 0.002` ≈ 1 s/mi at a 9:00/mi pace, which is the display
resolution of `_delta_text`'s pace row. Same argument, smaller effect (pace at
5.0: 23% → 24%).

`STAR_NOISE["hr"] = 0.0` and `["continuity"] = 0.0` **deliberately**:
- HR's cap axis already subtracts `HR_CAP_NOISE_BPM` upstream inside
  `hr_cap_severity`. Adding a second floor would move the 11.3 bpm boundary and
  force the zone-4+5 revalidation for nothing.
- HR's rolling-band axis has a *band*, not a point — its zero region is already
  3–14% wide.
- `continuity_deviation` already subtracts `CONTINUITY_TOLERANCE` (0.15 of
  slack).

### 1.4 The per-metric `STAR_SCALE` divergence I tested and rejected

The brief asked for `SCALE` values "derived from the percentiles". I derived
them, simulated them, and they lose. Two candidates, both with the noise floors:

| | distance floor rate | distance buckets | pace buckets | overall @5.0 | overall buckets | **old F-cap set caught** |
|---|---|---|---|---|---|---|
| **B (chosen)** SCALE 0.35 all | 6% | 17/17 | 15/17 | 17% | 12/17 | **10/10** |
| C: dist 0.55, pace 0.28 | 1% | 17/17 | 17/17 | 18% | 12/17 | **8/10** ✗ |
| D: dist 0.45, pace 0.30 | 3% | 17/17 | 16/17 | 18% | 12/17 | 10/10 |

C was the "natural joint" reading (distance's real gap is 0.528 → 0.740, so a
0.55 floor saturates only the two genuine outliers). It buys **+2 pace buckets
and +1 point of overall spread**, and it costs **2 of the 10 cards today's F-cap
catches** — it stops flagging cards the current rubric considers catastrophic.
That is a straight regression on a validated behavior for a rounding error's
worth of granularity. D buys +1 pace bucket out of 17 in exchange for moving two
constants with no external evidence behind the new values.

**Decision: hold all four at 0.35.** The dict is still per-metric — it is the
named place for the incommensurability, `calibrate_report_card.py`'s
`GOVERNING_CONSTANTS` must list it, and the rejection above belongs in a comment
on a live constant, not in a deleted branch.

This also answers **incommensurability flags #3 and #4**:
- **#3 continuity's raw excess.** `SCALE = 0.35` preserves the documented
  calibration verbatim ("20% slower = A, 35% = C, 55% = F" → 4.00 / 2.00 / 1.00
  stars). A continuity `d` of 0.35 and a distance `d` of 0.35 are still not the
  same physical thing — they never were, they shared `GRADE_BANDS` — but they
  are now the same *statement about severity*, which is what a shared readout
  can honestly claim. The 2.413 outlier (a real run/walk) saturates at 1.0
  rather than extrapolating to −22 stars.
- **#4 distance's sidedness.** Unchanged: two-sided on a plan, one-sided-low on
  the rolling median. The zero mass really is an artifact of which reference
  existed, and the fix is the noise floor (which makes the plan branch's 13 GPS
  rows read 5.0) plus printing the bound — *not* collapsing the gating, which is
  the direction-gating contract.

### Worked table (widen = 1.0 unless noted)

```
  d       widen=1.0        widen=0.6 (plan)   widen=1.5 (steady)
  0.000   5.00  (5.0)      5.00  (5.0)        5.00  (5.0)
  0.010   4.80  (4.75)     4.67  (4.75)       4.87  (4.75)
  0.025   4.50  (4.5)      4.17  (4.25)       4.67  (4.75)
  0.050   4.00  (4.0)      3.33  (3.25)       4.33  (4.25)
  0.075   3.50  (3.5)      2.75  (2.75)       4.00  (4.0)
  0.100   3.00  (3.0)      2.33  (2.25)       3.67  (3.75)
  0.150   2.50  (2.5)      1.67  (1.75)       3.00  (3.0)
  0.200   2.00  (2.0)      1.11  (1.0)        2.67  (2.75)
  0.275   1.50  (1.5)      1.00  (1.0)        2.17  (2.25)
  0.350   1.00  (1.0)      1.00  (1.0)        1.78  (1.75)
  0.500   1.00  (1.0)      1.00  (1.0)        1.11  (1.0)

continuity ratio -> stars
  1.15 (tolerance) 5.00 | 1.20 -> 4.00 | 1.28 -> 2.70 | 1.35 -> 2.00
  1.45 -> 1.33 | 1.50 -> 1.00 | 3.56 (the live run/walk) -> 1.00
```

---

## 2. GRANULARITY / QUANTIZATION

**Internal: continuous float.** Per-metric stars are stored in `card_json` at
`round(x, 3)`; `overall_stars` is a REAL column at `round(x, 3)`. 3 dp is chosen
so the value is deterministic across renders (the `card_store` UPSERT's
byte-identical-no-op contract requires it) while carrying more precision than
any display ever shows.

**Display: quarter star, half-up.**

```python
DISPLAY_STEP = 0.25

def display_stars(x: float | None) -> float | None:
    if x is None:
        return None
    # math.floor(x/step + 0.5), NOT round(): Python's round() is banker's, so
    # round(4.125/0.25) == 16 and the card would show 4.00 for a value 4.125
    # while the identical distance 0.125 above the next knot showed 4.50.
    return math.floor(x / DISPLAY_STEP + 0.5) * DISPLAY_STEP
```

Applied **at render only** — `render_markdown`, `visuals`, and the MCP payload's
`stars_display` field. The weighted mean in §5 consumes the *unquantized*
per-metric values; quantizing before averaging compounds four rounding errors
into the one number the user reads first.

**Why a quarter and not a tenth.** One quarter star spans `Δd` of 0.0125 in the
top band and 0.0375 in the bottom band (widen 1.0). On a 5-mile expectation that
is **0.063–0.19 mi**, against a measurement noise floor of 0.02 mi
(`_DISTANCE_ON_TARGET_MI`) — a 3× to 9× margin. A tenth star would span
0.025–0.075 mi, i.e. **1.2×** the GPS noise at the top of the scale: the card
would move a visible step for a difference it cannot measure. That is the
definition of false precision, and it is the same failure as grading a time
fraction through a magnitude table.

Quarter stars also give **17 distinguishable levels** across [1.0, 5.0] against
today's 5 base letters (13 with modifiers, but the modifiers were stripped before
they could reach the overall). And they are drawable: quarter-fill is the finest
partial fill a star glyph can render legibly at card size.

---

## 3. THE FLOOR

**`STAR_FLOOR = 1.0`. The worst score is one star, not zero.** Usable range
1–5, width 4.

Three reasons:

1. **Five-star is a read convention with a known bottom.** An empty row reads as
   "not rated", not "rated terrible". Zero stars and n/a would be visually
   identical on a card where `n/a` is a common, meaningful outcome (continuity
   abstains on **83%** of history; quality-day pace abstains whenever splits are
   missing). Those two states must not collide.
2. **A 1.0 floor makes the F-cap analogue land on a round number.** `1.0 + 2.0 =
   3.0`, which is exactly the C the current `_F_FLOOR_GRADE` caps to (C = 2.0
   GPA = 3.0 on a 1–5 scale). §4.
3. Nate asked for it.

**Effect on weights: none.** A weighted mean of values in [1, 5] is in [1, 5];
the weights are affine-invariant and `INTENT_METRIC_WEIGHTS` needs no change.
What *does* change is the interpretation of a weight's leverage: on a 0–4 scale a
weight of 0.42 could move the overall 1.68 points; on 1–5 it moves it 1.68
stars — same magnitude, different zero. Nothing downstream reads a ratio of
stars, and nothing should (a "50% better run" is not a thing this scale means).

**What a truly failed metric shows: 1.0, saturating.** A `d = 0.643` HR cap
breach (19.5 bpm sustained over the ceiling) and a `d = 2.413` continuity (a
literal run/walk, 3.56× the median split) both read exactly `★☆☆☆☆`. They are
not distinguished, and they should not be: past the floor the question stops
being "how much worse" and starts being "this was not the workout that was
prescribed", which is a binary. The severity that *is* still legible lives in the
row's own Delta cell (`+18.0 bpm`, `2.41x over`) and in the note. **Extrapolating
below 1.0 would be worse than useless** — continuity's max is 6.9× its floor, so
an unsaturated linear extension would put that one card at −22.6 stars and drag
any mean containing it into nonsense.

---

## 4. WHAT REPLACES THE F-CAP

```python
OVERALL_STAR_HEADROOM = 2.0

overall = min(weighted_mean, min(per_metric_stars) + OVERALL_STAR_HEADROOM)
```

**The overall can never be more than two stars above its worst graded row.**
That is the CLAUDE.md contract — "the card must never print an overall that
contradicts a row beside it" — expressed as an invariant of the arithmetic rather
than as a threshold that has to fire.

It reproduces the discrete rule exactly at the old boundary: an F row is 1.0
stars, so the cap is 3.0, which is the C that `_F_FLOOR_GRADE` produces today.
Between there and no-bite it degrades linearly instead of stepping.

### Tested against the 10 real F-cap cases

| headroom | fires on | mean drop | max drop | **catches today's 10 F-cap cards** |
|---|---|---|---|---|
| 1.5 | 66/236 (28%) | 0.54 | 1.61 | 10/10 |
| **2.0 (chosen)** | **29/236 (12%)** | **0.45** | **1.11** | **10/10** |
| 2.5 | 12/236 (5%) | 0.30 | 0.61 | **6/10** ✗ |

2.5 fails the only hard requirement: it silently stops capping 4 of the 10 cards
the current rubric considers contradictory. 1.5 fires on more than a quarter of
all cards and piles 20 of them on exactly 2.50 — it stops being a safety rail and
becomes the primary scoring rule.

**Bite breakdown at 2.0** (worst-row star of each capped card):

```
worst row: 1.00 -> 13 cards   (an F today; 10 of these were capped today, 3 already scored <= C)
           1.25 ->  2
           1.50 ->  5
           1.75 ->  3
           2.00 ->  3
           2.25 ->  2
           2.50 ->  1
new bites (worst row > 1.0): 16 cards, mean drop 0.37 stars, max drop 0.91
```

The 16 new bites are cards carrying a row at 1.25–2.50 stars — a D or a low C
today, which today's cap ignores entirely — being pulled down by a third of a
star on average. That is the continuous rule working: a card whose worst row is
"well off target" should not read 4.6 stars overall, and today it can.

**Rejected alternatives.** A soft penalty term (`mean − k·(mean − min)`) does not
guarantee the invariant, only nudges toward it; a card can still print an overall
that contradicts its worst row, which is the whole reason the cap exists. Keeping
a hard threshold ("if any row ≤ 1.0, cap at 3.0") reintroduces a cliff into a
scale whose entire point is that it has none — and it would leave the 16 new
bites uncaught.

**Reported, not silent.** `overall["capped"] = True` and
`overall["capped_by"] = {"metric": "hr", "stars": 1.0}` replace today's
`capped_by: "F"`. The uncapped mean stays on the card as `overall["mean_stars"]`
so the card can say *why* the two disagree — the same honesty
`overall_grade` already keeps by leaving `gpa` untouched when the letter is
capped.

---

## 5. THE OVERALL SCORE

```python
def overall_stars(metrics, cls="steady"):
    weights = INTENT_METRIC_WEIGHTS.get(cls, METRIC_WEIGHTS)
    pairs = [(weights[k], m["stars"]) for k, m in metrics.items()
             if m.get("stars") is not None and k in weights]
    if not pairs:
        return {"stars": None, "mean_stars": None, "graded_metrics": 0}
    total_w = sum(w for w, _ in pairs)
    mean = sum(w * s for w, s in pairs) / total_w
    worst_w, worst = min(pairs, key=lambda p: p[1])
    capped = min(mean, worst + OVERALL_STAR_HEADROOM)
    ...
```

Everything structural is unchanged and **verified to still work**:

- `INTENT_METRIC_WEIGHTS` is untouched. It is still what makes load structurally
  unable to move the overall (no `load` key → never iterated).
- n/a redistribution is unchanged: an ungradeable metric is not in `pairs` and
  `total_w` renormalizes. Verified on the corpus — continuity abstains on 195 of
  236 cards and its 0.15 redistributes exactly as today.
- Zero gradeable metrics returns `stars: None`, never 1.0. "Failed" and "we did
  not measure this" must not be the same number, same as today's `n/a` ≠ `F`.

**Two quantizations disappear.** Today the path is
`d → letter → base_letter (drops the modifier) → GRADE_POINTS → GPA → _GPA_CUTS →
letter`. The `base_letter` step is the expensive one: it exists explicitly "so
the weights stay the approved ones and a modifier can never move an overall
grade", which rounds every metric *up* to its band top before averaging. That is
why today's median GPA is 3.53 and 33% of cards read exactly 4.00. With a
continuous per-metric value there is no modifier to strip — the interpolated
value **is** the grade, and it should move the overall. This is a deliberate
reversal of an existing contract; see §11.6.

**Displayed precision of the overall: quarter star, same as the rows.** The card
prints a star row for the overall in the same glyph alphabet as each metric; a
tenth-star overall beside quarter-star rows would not reconcile visually, and the
"never compare two quantities" discipline applies to units of precision too.
Stored at 3 dp.

### Measured overall distribution (n=236)

```
  1.00    0
  1.25    0
  1.50    0
  1.75    1  #
  2.00    0
  2.25    0
  2.50    1  #
  2.75    3  ###
  3.00   24  ########################        <- the cap floor (see §11.3)
  3.25    5  #####
  3.50   15  ###############
  3.75   17  #################
  4.00   27  ###########################
  4.25   28  ############################
  4.50   35  ###################################
  4.75   40  ########################################
  5.00   40  ########################################

n=236  mean 4.20  median 4.36  p25 3.78  p75 4.77  min 1.72  max 5.00
```

vs today: **A 139 / B 67 / C 29 / D 1 / F 0**, GPA p75 = p90 = p95 = max = 4.00,
33% of cards at exactly 4.00.

Rank agreement with today's GPA: **concordant/(concordant+discordant) = 0.944**
over all 27,730 card pairs. The ordering is substantially preserved; the 5.6%
that invert are cards where the modifier `base_letter` threw away was the
difference.

Envelope by today's letter (new star, capped):

| today | n | min | p10 | median | max |
|---|---|---|---|---|---|
| A | 139 | 3.39 | 4.30 | **4.69** | 5.00 |
| B | 67 | 3.12 | 3.45 | **3.87** | 4.28 |
| C | 29 | 2.42 | 2.77 | **3.00** | 3.27 |
| D | 1 | — | — | **1.72** | — |

---

## 6. THE ZERO PILE

**A free-side 0.0 earns exactly 5.0 stars. Yes.**

CLAUDE.md 0.41.0 is explicit: direction gating means a run on the free side
scores an exact 0.0, the grades are right, and *"Don't fix an A+-inflation
complaint by touching `GRADE_BANDS`, `_modifier` or `PLAN_TIGHTEN`; it is a
display problem."* Rescaling the curve so that a compliant easy run reads 4.3
instead of 5.0 would be doing exactly that, one abstraction layer up. An easy day
run slower than its floor is **compliance**, and compliance is five stars.

So, plainly: **granularity on distance, HR and continuity comes only from the
penalized side and from the overall.** Quantified —

| metric | rows | zeros | @5.0 after | @floor | buckets used | today's letters |
|---|---|---|---|---|---|---|
| distance | 236 | 51% | **62%** | 6% | **17/17** | A162 B13 C22 D26 F13 |
| pace | 236 | 21% | **24%** | 0% | **15/17** | A131 B56 C45 D3 F1 |
| hr (all) | 236 | 87% | 89% | 1% | 14/17 | A218 B2 C9 D4 F3 |
| — hr rolling band | 225 | 90% | 92% | 0% | 12/17 | — |
| — hr prescribed cap | 11 | 27% | 27% | 27% | 7/17 | — |
| continuity | 41 | 71% | **73%** | 7% | 8/17 | A32 B1 C3 D2 F3 |
| **overall** | 236 | — | **17%** | 0% | **12/17** | A139 B67 C29 D1 |

Read that honestly: **HR cannot be granularized and neither can continuity.** 90%
of HR rows sit inside their band and 71% of continuity rows are inside tolerance.
Those metrics are *binary in practice on this athlete's data* — they fire rarely
and hard, which is what they are for. The metrics that carry the granularity are
**pace** (75% of rows strictly between floor and max, 15 levels — up from an
effective 3), **distance** (31% interior, 17 levels), and the **overall** (17% at
max down from 33%, 12 levels up from 4).

That is the answer to the brief's >50% test: distance and HR exceed 50% at max
and structurally must; pace does not (24%) and the overall does not (17%), and
those two are what the user actually reads first.

### Optional, additive, NOT a change to the gated deviation

**The single largest available granularity gain is not a curve — it is
`activity_splits` coverage.** Continuity grades 41 of 236 rows (17%) because
splits are written by the daily-sync ingest path and never by backfill; 194 of
the 195 abstentions say "no splits recorded". A splits backfill would take
continuity from 41 to ~236 graded rows and add a **fourth independent axis** to
every card, with a measured 27% non-max rate — more resolution than any curve
change in §1.4 could buy, at zero rubric risk. It is out of scope here and needs
its own design (Garmin rate limits, the same 429 constraint that shaped
`ingest/details.py`), but it should be recorded as the follow-on.

**Do not** manufacture free-side resolution. `hr_drift_pct` and the stimulus
block's `aerobic_pct` are already computed, already displayed, and already
non-graded — they give the free side prose texture without pretending to be a
score. Starring them would re-introduce exactly the double-counting the
0.40.0 compliance/stimulus partition removed.

---

## 7. HR'S TWO POPULATIONS

**One curve, one `STAR_SCALE["hr"] = 0.35`. No split.**

The two generating processes stay separate where they already are — upstream, in
`hr_deviation` (fractional distance outside a band edge) and `hr_cap_severity`
(bpm over a ceiling, shifted by `HR_CAP_NOISE_BPM`, scaled by
`HR_CAP_BPM_SCALE`). Both already emit a `d` on a common footing; that
normalization is the job `hr_cap_severity` was written to do in 0.40.2, and doing
it twice is how 0.40.0 broke.

**The 11.3 bpm F-floor is preserved bit-exactly** (§1, table). `1.5 + 0.35 × 28.0
= 11.3` still lands on `z = 1.0` still lands on `STAR_FLOOR`. **The zone-4+5
revalidation CLAUDE.md requires before those constants move is therefore not
required, because they do not move.** That preservation is the single strongest
argument for holding `STAR_SCALE` uniform, and it is why I rejected the
per-metric divergence in §1.4 even where it looked locally attractive.

Measured, the cap axis is the *best-resolved* sub-population on the card relative
to its own range: 11 rows, 7 distinct quarter-star buckets, 27% at 5.0, 45%
strictly interior, 27% at the floor. It uses the scale.

The band axis is the opposite (90% zeros) and that is correct — it is a
range, and sitting inside the range is the whole point.

**One caveat to record.** Just above `HR_CAP_NOISE_BPM` the curve is at its
steepest in bpm terms: 1.5 bpm → 5.00, 2.0 bpm → 4.64 (displays 4.75), so half a
bpm of time-weighted exceedance costs a quarter star. Nothing in the live data
occupies 1.5–4.55 bpm (the constant was calibrated to separate a 1.37 population
from a 4.55 one), so this region is **currently unpopulated** — but if a future
run lands there, that steepness is the first thing to look at, and the fix is
`HR_CAP_NOISE_BPM`, not the curve.

---

## 8. SIMULATION

Method: 240 real cards over a trailing 730 days regraded through
`load_report_card_inputs` → `build_card`, deviations extracted with the `widen`
each card actually used, then run through `stars_from_deviation` +
`overall_stars`. 236 cards had a usable reference. Histograms are at the display
quantization (quarter star, 17 buckets from 1.00 to 5.00).

### Per metric — proposed mapping (SCALE 0.35, noise floors, headroom 2.0)

```
distance: n=236 mean=4.07 med=5.00 @5.0=62% @floor=6% buckets=17/17
  1:15  1.25:4  1.5:7  1.75:10  2:5  2.25:5  2.5:4  2.75:7  3:4  3.25:1
  3.5:5  3.75:3  4:4  4.25:4  4.5:5  4.75:6  5:147

pace:     n=236 mean=3.99 med=4.27 @5.0=24% @floor=0% buckets=15/17
  1:1  1.25:0  1.5:2  1.75:1  2:0  2.25:5  2.5:10  2.75:14  3:21  3.25:15
  3.5:12  3.75:13  4:17  4.25:22  4.5:22  4.75:24  5:57

hr (all): n=236 mean=4.77 med=5.00 @5.0=89% @floor=1% buckets=14/17
  1:3  1.25:0  1.5:1  1.75:0  2:4  2.25:2  2.5:3  2.75:1  3:2  3.25:1
  3.5:0  3.75:1  4:1  4.25:1  4.5:2  4.75:5  5:209

hr (prescribed cap): n=11 mean=2.88 med=2.58 @5.0=27% @floor=27% buckets=7/17
  1:3  1.5:1  2.25:1  2.5:1  3:1  4.5:1  5:3

hr (rolling band):   n=225 mean=4.86 med=5.00 @5.0=92% @floor=0% buckets=12/17
  2:4  2.25:1  2.5:2  2.75:1  3:1  3.25:1  3.75:1  4:1  4.25:1  4.5:1  4.75:5  5:206

continuity: n=41 mean=4.31 med=5.00 @5.0=73% @floor=7% buckets=8/17
  1:3  1.5:2  2.5:2  2.75:1  4:1  4.25:1  4.75:1  5:30

OVERALL (uncapped mean): n=236 mean=4.26 med=4.38 @5.0=17% buckets=12/17
OVERALL (capped):        n=236 mean=4.20 med=4.36 @5.0=17% buckets=12/17
  1.75:1  2.5:1  2.75:3  3:24  3.25:5  3.5:15  3.75:17  4:27  4.25:28
  4.5:35  4.75:40  5:40
  cap fired 29/236 (12%), mean drop 0.45, max drop 1.11; old F-cap set 10/10 caught
```

### Side by side with today

| | today (letters) | proposed (stars) |
|---|---|---|
| distance | A 162 · B 13 · C 22 · D 26 · F 13 — **5 levels, 69% in one** | **17 levels**, 62% in one (the zero pile) |
| pace | A 131 · B 56 · C 45 · D 3 · F 1 — **5 levels, 56% in one** | **15 levels**, 24% max, mean 3.99 |
| hr | A 218 · B 2 · C 9 · D 4 · F 3 — **5 levels, 92% in one** | 14 levels, 89% max (structural) |
| continuity | A 32 · B 1 · C 3 · D 2 · F 3 — 5 levels, 78% in one | 8 levels, 73% max (structural, n=41) |
| **overall** | **A 139 · B 67 · C 29 · D 1 · F 0 — 4 levels, 86% in two** | **12 levels**, top bucket 17%, IQR 3.78–4.77 |
| perfect score | GPA exactly 4.00 on **80/240 (33%)** | 5.00 on **40/236 (17%)** |
| F-cap / cap | 10/240 (4%) | 29/236 (12%), all 10 included |

The share at a perfect score **halves**, the overall goes from 4 occupied levels
to 12, and pace — the metric that carries most easy/quality days at 0.42 weight —
triples its resolution. Distance and HR do not improve at the top and cannot;
that is §6.

### Eval scenarios re-scored

`tests/evals/report_cards.py` driven through the same mapping:

| scenario | today | GPA | dist | pace | hr | cont | mean | **capped** | bound |
|---|---|---|---|---|---|---|---|---|---|
| obedient_easy_clean | A | 4.00 | 5.00 | 5.00 | 5.00 | 5.00 | 5.00 | **5.00** | min A |
| obedient_easy_straddling | A | 4.00 | 5.00 | 5.00 | 5.00 | 5.00 | 5.00 | **5.00** | min B |
| cap_blown_hard | C | 3.04 | 5.00 | 5.00 | **1.00** | 5.00 | 4.04 | **3.00** | max C |
| interval_manual_laps | A | 4.00 | 5.00 | 5.00 | 5.00 | n/a | 5.00 | **5.00** | min B |
| walk_mislabelled | A | 3.62 | 3.00 | 4.25 | 5.00 | 5.00 | 4.30 | **4.25** | min B |

Every declared bound survives under the §10 translation, and the ordering test
holds: clean 5.00 ≥ straddled 5.00 > blown 3.00, with the HR row itself 5.00 vs
1.00. `cap_blown_hard` demonstrates the new cap doing exactly the old cap's job —
a 4.04 mean pulled to precisely 3.00 by one floored row.

---

## 9. THE CALIBRATION GATE

`scripts/calibrate_report_card.py` keeps its shape, its read-only URI, its
manual-not-CI status, and `test_the_gate_is_not_wired_into_ci`. `LETTERS`
becomes the 17 quarter-star buckets; the histogram row becomes a compact
occupancy strip. Two signatures, restated:

### 9.1 Punitive skew — direct translation

`D` and `F` are `points ≤ 1`, which is `stars ≤ 2.0`.

> **FAIL if more than `--max-fail-share` (default 0.60) of graded rows score
> ≤ 2.0 stars.**

Threshold unchanged, semantics unchanged. Measured today: distance 17%, pace 2%,
hr band 2%, hr cap 36%, continuity 12% — all pass, with the same margins the
letter version had.

### 9.2 Dead bands → **collapsed scale**

"Dead bands" has no honest continuous analogue: with 17 buckets and n=41,
continuity would trivially "fail" on 9 unused buckets while being perfectly
healthy. Counting occupied buckets is the wrong measure.

What actually broke in 0.40.0 was **bimodality** — mass piled at both extremes
with nothing between. So gate that directly, on two quantities:

- `floor_share` = rows displaying ≤ 1.0 stars
- `interior_share` = rows with `1.0 < stars < 5.0` (strictly between)

> **FAIL if `floor_share ≥ 0.25` AND `interior_share < 0.25`.**

Both conditions are required, and the conjunction is what preserves the gate's
deliberate asymmetry. A metric can be 92% at 5.0 (hr rolling band) and pass,
because concentration in a *good* score is reported and never gated — that
asymmetry was this check's original correction to its own first draft and it
survives verbatim. A metric only fails when it is *both* punishing heavily *and*
refusing to grade anything in between, which is precisely "the table has stopped
discriminating".

Verification, on today's data and on the known-broken axis:

All three quantities are computed on the **display-quantized** value, so the gate
judges exactly what a reader sees.

```
line                     n  floor%  interior%   <=2.0%  ==5.0%  buckets  verdict
distance               236      6%        31%      17%     62%       17  ok
pace                   236      0%        75%       2%     24%       15  ok
hr (rolling band)      225      0%         8%       2%     92%       12  ok
hr (prescribed cap)     11     27%        45%      36%     27%        7  ok
continuity              41      7%        20%      12%     73%        8  ok

0.40.0's broken HR-cap time-fraction axis (32 F / 7 A / 4 D, replayed):
                        43     74%         9%      84%     16%        3  FAIL
                                              -> punitive skew AND collapsed scale
```

**Both signatures still catch the failure the gate was written for**, and nothing
healthy false-fails. `hr (rolling band)` at 8% interior is the one that would
have tripped a naive interior-only rule — its `floor_share` of 0% is what saves
it, which is the conjunction earning its keep.

### 9.3 Reported, not gated

- Share at exactly 5.0 per line (the asymmetry: high is fine, and it is the
  number the user experiences as "does this thing ever say anything").
- Occupied bucket count and IQR — diagnostics for a human, not thresholds.
- Overall distribution and cap-fire rate, still informational only: the overall
  is derived from the rows, so gating it reports one defect twice. The current
  script already prints F-cap rate for exactly this reason; it becomes cap rate.

`GOVERNING_CONSTANTS` gains `STAR_KNOTS`, `STAR_SCALE`, `STAR_NOISE` on every
line and `OVERALL_STAR_HEADROOM` on the overall report.

`MIN_SAMPLE = 10` is unchanged; `hr (prescribed cap)` at n=11 stays gated.

---

## 10. MIGRATION OF MEANING

### Stored cards: left as historical letters, not re-scored, not recomputed on read

`card_store`'s contract is that a stored row is *"the card as actually shown,
graded against the plan active at that render"* — a historical record, with no
backfill path by design and exactly one sanctioned mutation
(`migrate_read_section_names`, which *"rewrites `card_json`'s key name and NOTHING
else — never `graded_at`, the key or a grade"*). Re-scoring rewrites history under
a rubric that did not exist at render time, and recomputing on read makes the
stored row a cache of nothing.

**No migration code is needed, because the existing mechanism already handles
it.** A star rubric changes `report_card.py`, which changes the read prompt
(§10.3), which changes `read_cache_key`, which invalidates all stored reads.
CLAUDE.md's standing rule for exactly this: *"After a release that touches
`report_card.py` or `workout_coach.py`, warm the stored cards — `uv run python
scripts/warm_report_cards.py`"*. Every stored card re-renders under the star
rubric on the next warm, and the UPSERT (keyed on the read's prompt key) writes a
fresh row with stars. **Ship the warm as part of the release, not after it.**

### Schema

Guarded-`ALTER` pattern in `db.init_schema` (the same one `source` uses):

```sql
ALTER TABLE report_cards ADD COLUMN overall_stars REAL;
```

`overall_grade` / `gpa` / `*_grade` columns are **kept and stop being written**
(NULL on new rows). Do not drop them: they are the only record of what the
pre-star cards said, and `card_json` for those rows carries letters too. Add
`distance_stars` / `pace_stars` / `hr_stars` / `continuity_stars` REAL alongside
if the query tools want them without decoding `card_json` — `list_report_cards`
currently reads the letter columns and will need star equivalents.

### The coach's memory aggregate (`ledger.report_card_facts`)

Today: `count`, `mean_gpa`, `grade_counts` (base-letter histogram), `trend`
(`interpret.pct_change` + `delta_direction` over two halves of a 21-day window).
Becomes:

- `mean_gpa` → **`mean_stars`**, `round(…, 2)`. Same computation, different
  column (`overall_stars`).
- `grade_counts` → **`star_counts`**, a **5-bucket band histogram**, not the
  17 quarter-star buckets: the ledger renders into a prompt block that is read
  aloud, and "two at 4.25, one at 4.5, one at 4.75" is noise in prose. Buckets
  are `STAR_VERDICT_CUTS` (§10.3): `≥4.25`, `≥3.5`, `≥2.5`, `≥1.5`, `<1.5`.
- `trend` is unchanged — `pct_change` over means works identically on a 1–5
  scale (both halves are on the same scale, and the ratio's zero point never
  enters `delta_direction`'s sign).

**Every invariant that made this fact safe to inject survives**: still an
aggregate of numbers only (never `card_json`, never `coach_read`), still
restricted to `activity_date < today`, still idempotent under an equal-key
re-save. Nothing about the star change touches the self-render-cascade analysis.

**Rows written before the change have `overall_stars IS NULL` and are skipped**,
exactly as rows with no `gpa` are skipped today (`if row.get("gpa") is None:
continue`). The window is 21 days, so the aggregate is fully star-based within
three weeks — and immediately, if the release warms the cards as instructed. Do
**not** synthesize a star from a stored letter to bridge the gap: mixing two
scales in one mean is the exact category error the module keeps getting burned
by, and a briefly-quiet memory line is cheaper than a wrong one.

`ledger.notables`' `bad_grade_dates` filter (`overall_grade[0] in ("D","F")`)
becomes `overall_stars <= 2.0` — the same `≤ 2.0` boundary the calibration gate
uses for punitive skew, so the two cannot drift.

### 10.3 One severity vocabulary, used in three places

```python
STAR_VERDICT_CUTS = ((4.25, "on target"),
                     (3.50, "slightly off target"),
                     (2.50, "off target"),
                     (1.50, "well off target"))
# below 1.50: "missed badly"
```

Cuts are `workout_coach._GRADE_SEVERITY`'s existing words, placed to reproduce
today's letter partition on the real corpus: `≥4.25` covers 54% of cards (today
A = 58%), `≥3.5` covers 83% (today A+B = 86%), `≥2.5` covers 99% (today
A+B+C = 98%).

One table, three consumers:

1. **`grade_severity(stars)`** for the coach prompt. `workout_coach.build_prompt`
   already passes severity words rather than letters — that is 0.28.1's fix for
   the 3.1% grade-leak rate — so the prompt shape does not change, only the
   lookup's input type.
2. **`find_grade_leak`** must be retargeted. `_GRADE_LEAK` hunts letters; the
   leak to prevent now is the model naming a star count, since the stars print in
   the table directly below the read. New pattern: a number in 1–5 (optionally
   fractional) adjacent to `star`/`stars`/`★`/`/5`, plus the surviving letter
   pattern for at least one release (a model with the old rubric in its weights
   will still reach for "an F"). `tests/test_workout_coach.py`'s two validated
   lists — 5 real leaks, 7 lookalikes — must be extended with star lookalikes
   before the pattern moves; "5 stars" is a leak, "the 5 miles" is not.
3. **`tests/evals/report_cards.EXPECTED_VERDICTS`**: `{"min": "B"}` →
   `{"min_stars": 3.5}`, `{"max": "C"}` → `{"max_stars": 3.5}`. Bounds stay
   bounds — the deliberate design that lets them survive recalibration — and all
   five current scenarios pass under this translation (§8). `_points()` in
   `test_report_card_verdicts.py` disappears; comparisons are on floats.

---

## 11. RISKS — what letters got right that this can get wrong

**11.1 A letter carries a shared social meaning; 4.25 does not.** Everyone knows
what a C is. Nobody has an instinct for 3.0/5, and "3 out of 5" reads worse than
"C" to most people even though they are the same verdict. *Mitigation:* the
severity word from `STAR_VERDICT_CUTS` prints beside the star row on the card,
not only in the coach prompt. It is one word and it costs one cell.

**11.2 The A+-inflation complaint returns, inverted.** A run that read `A` for a
genuine 4.9% miss now reads 4.02 stars — more honest, and it will feel like a
demotion. Worse, a *two-sided* plan target can essentially never read 5.0 without
the noise floor (0 exact zeros in 17 rows). **The noise floor is the load-bearing
mitigation, not polish**, and if a future change removes it, every GPS-rounding
plan day starts printing 4.75 next to a Delta cell reading `on target`. The
free-side zeros still read exactly 5.0, which is the property 0.41.0 actually
protected.

**11.3 The cap puts 24 of 236 cards (10%) on exactly 3.00 — a visible spike on a
"continuous" score.** It is inherited from `STAR_FLOOR`: 13 cards have a floored
row, and `1.0 + 2.0` is one number. It is the honest analogue of today's pile at
C and it is not a bug. **Do not "smooth" it by unsaturating the floor** — §3
explains why (continuity's max is 6.9× its floor). If it ever needs softening,
the knob is `OVERALL_STAR_HEADROOM`, and §4's table is the evidence for what that
costs.

**11.4 False precision will be requested.** Someone will ask for tenths, or for
the overall at two decimals. §2's arithmetic is the standing answer: one tenth
star is 1.2× the GPS noise floor at the top of the distance scale. Write it into
the constant's comment, because the request will come from the person who wrote
this spec.

**11.5 The bottom of the scale is unreachable and the gate must not care.**
Minimum overall across 240 real cards is **1.72**; buckets 1.00–1.50 are empty
and always will be, because a weighted mean of four metrics cannot floor unless
all four floor. Today's overall never emitted an F either, so this is not a
regression — but the §9.2 collapsed-scale signature must stay off the overall
(it is informational only), or the gate will fail on healthy data forever.

**11.6 A deliberate contract is being reversed: the modifier now moves the
overall.** `base_letter` exists specifically "so the weights stay the approved
ones and a modifier can never move an overall grade". With a continuous value
there is no modifier to strip, so the sub-band position *does* propagate. That is
the point — it is where most of the new granularity comes from — but it means
5.6% of card pairs reorder relative to today's GPA (concordance 0.944), and a
card the user remembers as "an A" can come back at 3.39 stars. *Mitigation:* warm
the cards in the same release so the change lands once, not card-by-card over
weeks as people happen to open them.

**11.7 `widen` becomes visible.** A 5% miss is 4.00 / 3.33 / 4.33 stars depending
on the yardstick (§1). Today all three could print "B" and nobody noticed. Two
cards side by side will now look inconsistent unless the reference is adjacent to
the score. *Mitigation:* `reference` moves into the metric row, not the footer.
*Residual risk:* this is a real UX cost of the change and it is not fully
eliminable — it is the price of keeping `PLAN_TIGHTEN`, which is a price worth
paying (§1).

**11.8 Splits availability now visibly costs stars.** Continuity grades on 17% of
history and floors on 7% of the rows it does grade — the highest floor rate of
any metric (distance 6%, hr 1%, pace 0%) — so it is the most likely cap trigger
on any card that has splits at all. Cards from synced days
are systematically harsher than backfilled ones. This is true today and stars
make it legible — which is an argument for the §6 backfill, not against the
change.

**11.9 Continuous does not mean calibration-free.** The curve is a display
transform of `d`; every deviation function and every constant feeding it still
decides what the card says. `scripts/calibrate_report_card.py` must be run before
touching any of them, and §9's gate is the only thing standing between a
plausible-looking curve and another 0.40.0. The one thing this design buys on
that front is that a *collapsed* scale is now much harder to ship unnoticed: the
0.40.0 axis emitted 3 distinct outcomes across 43 runs and would fail both
signatures.

**11.10 One thing letters got structurally right that stars do not.** A letter is
ordinal and makes no claim about *distance* between grades; a star is interval
and invites arithmetic ("this run was 20% better"). It is not — the curve is
piecewise-linear in `d`, not in anything physical, and `d` itself means four
different things across the four metrics. Nothing in the code should ever
subtract two stars and call the result a magnitude, and `ledger`'s `pct_change`
over `mean_stars` (§10) is the one place that comes close: it compares two means
on the identical scale, which is legitimate, and it must not be generalized into
comparing a distance star against an HR star.

---

# Star rating — UX / presentation spec

Design only. Every claim below was measured; the measurement is stated inline.
Prototypes: `star_svg.py`, `starmath.py`, `card_proto.py`, `hero_b.py`,
`glyphtests.txt`, rendered PDFs `heroB_*.pdf` / `svg_probe.pdf` / `size_probe.pdf`
in this directory.

---

## 0. What I measured first (and what it forced)

**Glyph coverage, from the cmaps, not from expectation.**

| glyph | IBM Plex Mono SemiBold (Nate's `data/brand.json` `mono_file`) | DejaVu Sans Mono (what CI has — `fonts-dejavu-core` is the ONLY font `.github/workflows/ci.yml` installs) |
|---|---|---|
| ★ U+2605 / ☆ U+2606 | **ABSENT** | present (regular + bold; **ABSENT in the Oblique faces**) |
| ◐ ◕ ● ○ | **ABSENT** | present |
| █ ▉ ▊ ▋ ▌ ▍ ▎ ▏ | present | present |
| ¼ ½ ¾ ⅓ ⅔ | present | present |
| ⭐ U+2B50 | ABSENT | ABSENT |
| ⯪ U+2BEA (half-black star) | ABSENT | ABSENT |

The brand mono has 963 codepoints and no star in any of them. A `★` in the PDF
therefore renders **through Pango per-glyph fallback**, not through the brand
font. I rendered it: it does appear (probe row 1, `probe-1.png`) but at a
visibly non-monospace advance, in a face that is not the brand's. That is a
render whose appearance depends on which machine it ran on — the same class of
problem as `img.split-chart` shipping without its height cap.

**So the PDF does not use star glyphs at all. It draws them.**

**Inline SVG works in WeasyPrint 69, and so does `clipPath`.** Measured
(`svg_probe.pdf`): a 0 / .25 / .5 / .75 / 1 fill ramp renders exactly.
`<img src="data:image/svg+xml;base64,…">` rendered **nothing** — the row was
blank. Inline `<svg>` is the mechanism; the data-URI image variant is not.

**A naive x-clip of a star is a lie.** A star's ink is not uniform in x.
Clipping the fill rect to 75% of the bounding box leaves **89.6%** of the ink,
which is why my first 4.75 looked like a 5.00. `starmath.py` solves the cut
that leaves exactly *f* of the polygon area:

```
   f     naive-x   ink@naive     area-x   ink@area
 0.125     3.47       0.021        5.90      0.125
 0.250     5.65       0.104        7.42      0.250
 0.375     7.82       0.284        8.82      0.375
 0.500    10.00       0.500       10.00      0.500
 0.625    12.18       0.716       11.18      0.625
 0.750    14.35       0.896       12.58      0.750
 0.875    16.52       0.979       14.10      0.875
```

The clip table is precomputed constants; no geometry solver ships.

---

## 1. THE GLYPH DECISION (terminal / markdown)

`★` and `☆` are `East_Asian_Width=Ambiguous`. So is `¼ ½ ¾` and so is every
block element. `◐ ◕ ◔` are `Neutral`. `⭐` is `Wide`. **Mixing width classes is
what breaks a markdown column**, because whatever a terminal decides
"ambiguous" means, it decides it for every ambiguous glyph alike.

Six candidates, rendered literally, with the EAW pattern per row:

```
--- A  integer stars + numeral                --- B  quarter fractions
    5.00  ★★★★★ 5.00                              5.00  ★★★★★
    4.88  ★★★★☆ 4.88                              4.88  ★★★★★     <- rounds UP to 5
    4.75  ★★★★☆ 4.75                              4.75  ★★★★¾
    4.50  ★★★★☆ 4.50                              4.50  ★★★★½
    4.25  ★★★★☆ 4.25                              4.25  ★★★★¼
    4.00  ★★★★☆ 4.00                              4.00  ★★★★☆
    3.60  ★★★☆☆ 3.60                              3.60  ★★★½☆
    2.75  ★★☆☆☆ 2.75                              2.75  ★★¾☆☆
    1.60  ★☆☆☆☆ 1.60                              1.60  ★½☆☆☆
    0.80  ☆☆☆☆☆ 0.80                              0.80  ¾☆☆☆☆
  EAW: 1 pattern -> ALIGNED                      EAW: 1 pattern -> ALIGNED
  FAILS: 4.88/4.75/4.50/4.25/4.00 identical      FAILS: no numeral, 4.88 == 5.00

--- C  eighth-resolution block bar             --- D  quadrant circles
    5.00  █████ 5.00                               5.00  ★★★★★
    4.88  ████▉ 4.88                               4.88  ★★★★★
    4.75  ████▊ 4.75                               4.75  ★★★★◕
    4.50  ████▌ 4.50                               4.50  ★★★★◐
    4.25  ████▎ 4.25                               4.25  ★★★★◔
    4.00  ████▏ 4.00                               4.00  ★★★★☆
    3.60  ███▋▏ 3.60                               3.60  ★★★◐☆
    2.75  ██▊▏▏ 2.75                               2.75  ★★◕☆☆
    1.60  █▋▏▏▏ 1.60                               1.60  ★◐☆☆☆
    0.80  ▊▏▏▏▏ 0.80                               0.80  ◕☆☆☆☆
  EAW: 1 pattern -> ALIGNED                      EAW: 4 patterns -> RAGGED
  FAILS: not stars; unit ticks invisible         FAILS: ragged; absent from BOTH fonts

--- E  emoji                                   --- F  half-star quantisation
    5.00  ⭐⭐⭐⭐⭐ 5.00                             5.00  ★★★★★ 5.00
    4.75  ⭐⭐⭐⭐· 4.75                             4.75  ★★★★★ 4.75   <- rounds UP
    2.75  ⭐⭐··· 2.75                             2.75  ★★★☆☆ 2.75   <- rounds DOWN
  EAW: 6 patterns -> RAGGED                      EAW: 1 pattern -> ALIGNED
  FAILS: ragged (W vs A); colour clashes         FAILS: rounds the partial away
```

Rejections: **D and E are ragged** — column alignment breaks between rows, and
D's glyphs are in neither font. **A and F round the partial away**, which is the
one thing the brief forbids. **C** is the only candidate that resolves 4.88 from
4.75 in the glyph itself and it has flawless coverage — but I measured it in the
PDF too (`probe-1.png` row 2) and adjacent full blocks **merge into one solid
slab with no visible seam**, so you cannot count five of anything. It is a
progress bar wearing a rating's job.

### PICK: `★★★★¾ 4.75` — quarter-quantised star row + exact 2-dp numeral.

Quantisation rule (this is the contract, not a rounding convenience):

```
whole = floor(score)
frac  = score - whole
if frac == 0            -> tail is ☆ (or nothing, if whole == 5)
else                    -> tail is ¼ | ½ | ¾, nearest quarter, CLAMPED to [¼, ¾]
```

The clamp is the load-bearing half. **A full star means the metric earned the
full star, and a partial star is always visibly partial** — 4.99 renders
`★★★★¾ 4.99`, never `★★★★★`; 4.01 renders `★★★★¼ 4.01`, never `★★★★☆`. Without
it, "no rounding the partial away" survives at the .5 boundary and dies at .01
and .99, which are exactly the values this distribution produces.

**Include the numeral. Always, 2 dp.** Four reasons, and any one of them
settles it: (a) quarter quantisation cannot separate 4.88 from 4.75, and both
are common in a distribution where a quarter of cards are perfect; (b) the
markdown is read aloud by an agent, and `★★★★¾` is not speech; (c) the card
already prints a number today (`4.00 GPA`), so removing it is a regression in
precision, not a simplification; (d) it is 5 characters.

Alignment: every glyph in the set is `EAW=Ambiguous`, so all ten rows above
share one width pattern — a half-star cannot shift a column relative to a full
one. `render.render_table` does not pad cells, so alignment is decided by the
terminal's markdown renderer from these widths; one width class is what makes
that stable.

### What the markdown surface becomes

`render_markdown` today:

```
## Overall: A (4.00 GPA)
| Metric | Actual | Expected | Delta | Grade |
| Distance | 5.01 mi | 5.00 mi | on target | A+ |
```

becomes:

```
## Overall: ★★★★¾ 4.75 / 5
_5 stars = you did what the day prescribed — a compliance score, not a verdict
on how good the run was._

| Metric | Actual | Expected | Delta | Rating |
| Distance | 5.01 mi | 5.00 mi | on target | ★★★★★ 5.00 |
| Pace | 9:44/mi | ≥ 9:39/mi | 5s/mi slower | ★★★★¾ 4.80 |
| Avg HR | 139 bpm | ≤ 140 bpm | 1 bpm under | ★★★★½ 4.50 |
| Continuity | — | — | — | n/a |
```

Header `Grade` → `Rating`. `METRIC_TABLE_HEADERS` is already shared by both
renderers; change it there and both move together.

---

## 2. THE PDF DESIGN

### 2a. The star renderer

ONE pure function in `visuals.py`, returning ONE `<svg>` per row (one element,
one id namespace, no per-star `<img>`):

```python
_STAR_PATH = ("M10 1.4 12.6 6.9 18.7 7.7 14.2 11.9 15.4 18 10 15.1 "
              "4.6 18 5.8 11.9 1.3 7.7 7.4 6.9Z")
#: Area-linearised clip x for each eighth. A star's ink is not uniform in x —
#: clipping at 75% of the BOUNDING BOX leaves 89.6% of the ink, which made a
#: 4.75 indistinguishable from a 5.00. These are the x where the ink actually
#: is f. Derived once (scripts/, not at runtime); do not "simplify" to 20*f.
_STAR_CUTS = {0.125: 5.90, 0.25: 7.42, 0.375: 8.82, 0.5: 10.0,
              0.625: 11.18, 0.75: 12.58, 0.875: 14.10}

def star_row(score, *, uid, em, ink, max_stars=5, quantum=8):
    ...
```

Markup it emits (5 stars, 4.75, ink):

```html
<svg class="stars" viewBox="0 0 116 20" height="1.15em" width="6.67em"
     role="img" aria-label="4.75 out of 5">
  <defs><clipPath id="m3_4"><rect x="0" y="0" width="12.58" height="20"/></clipPath></defs>
  <g transform="translate(0,0)">
    <path d="…" fill="none" stroke="#181510" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="…" fill="#181510"/>
  </g>
  <!-- …three more full… -->
  <g transform="translate(96,0)">
    <path d="…" fill="none" stroke="#181510" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="…" fill="#181510" clip-path="url(#m3_4)"/>
  </g>
</svg>
```

Every star always gets the outline; the fill is what is clipped. So the empty
portion is an **ink rule**, which is the PRESS idiom — no tint, no track, no
gradient, no rounded corner. `stroke-linejoin="round"` is on the *join*, not a
corner radius; it keeps the 36° points from spiking at small sizes.

`uid` must be unique per row — WeasyPrint resolves `clip-path: url(#id)` in
document scope. Collide the ids and one row clips another.

CSS:

```css
svg.stars { vertical-align: -0.12em; }
td.metric-grade { white-space: nowrap; text-align: left; }
span.star-num  { font-family: {mono}; font-size: 0.95em; color: {dim};
                 margin-left: 0.45em; letter-spacing: 0.02em; }
span.star-num.crit { color: {accent}; }
span.stars-na  { color: {dim}; font-style: italic; }
```

`td.metric-grade`'s `font-weight: 900; font-size: 1.15em` goes away — weight was
how a letter got its emphasis, and an SVG has its own. `_grade_class` and the
`.grade-A … .grade-F` rules go with it, replaced by the single `.crit` state
below.

### 2b. The accent

Today `.grade-D, .grade-F { color: accent }`, applied to `td.metric-grade` **and**
`td.grade-letter`. Star equivalent, preserving the semantic exactly:

```python
STAR_CRITICAL = 2.0   # the old D/F territory
```

A metric row scoring **below `STAR_CRITICAL`** draws its stars and its numeral
in the accent. Everything else is ink. Nothing else on the card gains an accent.
Verified on a real render (`hb_23825963527-1.png`, the 2026-08-02 easy day, four
metrics at 5.00): **the only orange on the page is the masthead stamp.** Zero
orange on a good day, kept.

On the one genuinely bad card in the store (`hb_23683001107-1.png`, 3.23 of 5.00
miles at 15:29/mi against a 6:58 target) the two failed rows are accent and the
rest is ink — the same two cells that are orange today.

Set `STAR_CRITICAL` to whatever score corresponds to the old D-band boundary
once the scoring lands; 2.0 is the value if the map is linear
(`score = 1 + GPA`). Across 240 cards this fires on **1 overall grade and a
handful of rows** — that rarity is the feature.

### 2c. The hero when the overall is 4.75

Today: a giant letter in a 22%-wide cell, with the GPA line and four read
paragraphs in a 74% cell beside it. **That layout cannot hold stars.** A 5-star
row is 5.8:1; at the current `hero_em` (5.2em roomy) it is ~34% of the text
width. I rendered it in the 22% cell and it overflows straight through the read
paragraphs (`c_23708627990_stars-1.png` — the stars sit on top of the HEART RATE
sentence).

**Restructure to a stacked hero** (rendered: `hb_*.pdf`):

```html
<div class="star-hero">
  <div class="band">
    <svg class="stars" …/><span class="hero-score">4.75</span><span class="hero-outof"> / 5</span>
    <p class="hero-meta">5.01 mi in 48:43 · 9:44/mi · easy (plan, 25 walks excluded)</p>
    <p class="hero-scale">5 stars = you did what the day prescribed. It is a
       compliance score, not a verdict on how good the run was.</p>
  </div>
  <!-- the four coach paragraphs, now FULL WIDTH -->
</div>
```

```css
div.star-hero .band { border-bottom: 2px solid {rule}; padding-bottom: 0.45em;
                      margin-bottom: 0.5em; }
span.hero-score  { font-family: {mono}; font-weight: 700; letter-spacing: -0.02em;
                   font-size: {0.62 * star_em}em; vertical-align: 0.06em; }
span.hero-outof  { font-family: {mono}; color: {dim}; font-size: {0.30 * star_em}em; }
p.hero-meta      { font-family: {mono}; font-size: 0.78em; letter-spacing: 0.06em;
                   color: {dim}; margin: 0.35em 0 0 0; }
p.hero-scale     { font-family: {serif}; font-style: italic; color: {dim};
                   font-size: 0.8em; margin: 0.15em 0 0 0; }
star_em = density["hero_em"] * 0.42          /* stays on the density ladder */
```

The ink rule under the band is the PRESS structural device that the giant letter
used to provide by mass. The read moving full-width is not cosmetic: it is what
pays for the star row's height (fewer line-breaks per paragraph), and it is why
the ladder barely moves — see §3.

The read paragraphs and the star row must stay adjacent, because the hero is
where the scale sentence lives and the prose sits directly beside the score.

---

## 3. WIDTH / ONE-PAGE ANALYSIS

**Raw width.** A grade cell holds `A+` — 2 mono characters, ~1.2em of the table
font. A star cell holds a 116×20 SVG at `height: 1.15em` (⇒ `width: 6.67em`) plus
a space plus `5.00` (~2.4em): **~9.4em, about 7.8× the ink**. On a 17.6 cm text
column with the table at `0.8em` of a 10.4pt body, that is **+68 pt ≈ +2.4 cm ≈
+13 percentage points** of table width.

**Colgroup.** `26/19/19/22/12` → **`23/19/19/17/22`**. Take the width from
Metric (26→23; the longest label is `Continuity`) and Delta (22→17), and leave
Actual and Expected at 19% — those carry `15:29/mi best mile` and `≤ 145 bpm`
and already wrap at 17%. Delta is the column to watch: `511s/mi slower` is the
longest cell in the store. I swept three colgroups (`23/19/19/17/22`,
`23/17/17/19/24`, `24/18/18/18/22`) and the ladder outcome was identical for all
three, so this is a legibility choice, not a fitting one.

**Ladder impact — measured, not estimated.** All 16 stored cards re-rendered
twice in one process through the real `visuals.fit_one_page` +
`CARD_DENSITY_PRESETS`, with the real HR split chart, letters vs stars
(`card_proto.py`):

| card | date | letters | stars |
|---|---|---|---|
| 23836824074 | 2026-08-03 | rung 2 | rung 2 |
| 23825963527 | 2026-08-02 | rung 2 | rung 2 |
| 23778992014 | 2026-07-29 | rung 2 | rung 2 |
| 23767829677 | 2026-07-28 | rung 0 | rung 0 |
| 23754187710 | 2026-07-27 | rung 0 | rung 0 |
| 23744574651 | 2026-07-26 | rung 0 | rung 0 |
| 23741747932 | 2026-07-26 | rung 0 | rung 0 |
| 23708627990 | 2026-07-23 | rung 3 | rung 3 |
| **23695862040** | **2026-07-22** | **rung 0** | **rung 1** |
| 23685126977 | 2026-07-21 | rung 1 | rung 1 |
| 23683001107 | 2026-07-21 | rung 0 | rung 0 |
| 23670110573 | 2026-07-20 | rung 0 | rung 0 |
| 23656638713 | 2026-07-19 | rung 0 | rung 0 |
| 23647919460 | 2026-07-18 | rung 0 | rung 0 |
| 23635100333 | 2026-07-17 | rung 1 | rung 1 |
| 23624321130 | 2026-07-16 | rung 0 | rung 0 |

**15/16 keep their winning rung. One (2026-07-22) drops exactly one, roomy →
compact. Every card is 1 page, including 23708627990 which already needs the
`ultra` rung and still fits there.** The ladder absorbs the change with a rung to
spare; no new rung is needed, and `CARD_DENSITY_PRESETS` must not gain one (the
brief's ladder exhaustion is a signal `generate_brief_report` reads).

Two consequences to write down:

- `_fit_with_hint` starts at `hint - 1`. The 2026-07-22 card converges to its new
  rung over one render, which is exactly the documented cost of the hint. No
  change needed.
- The star SVG scales with `hero_em` and `1.15em` of the table font, so it rides
  the ladder rather than being a fixed-pt island. This is the `chart_h_pt` /
  `img.split-chart` lesson: **a new page element that does not read the density
  preset makes the ladder decorative for that element.** `star_em =
  density["hero_em"] * 0.42` is the hero's hook; the table's `1.15em` is the
  table's.

**Add a case to `test_report_card_is_always_exactly_one_page`** for a 5-metric
card whose every row is 5.00 (widest possible rating column) with a 14-split
activity and a 969-char read — that is the worst case this change creates, and
it is not in the store.

---

## 4. THE n/a STATE, AND THE CAP

**n/a is on most cards** (continuity has no splits on 83% of history). It must
never render as `☆☆☆☆☆`, which is a score of zero and is the loudest possible
statement of failure on a row where nothing was measured.

- markdown: `n/a` in the Rating cell, `—` in Actual/Expected/Delta (unchanged).
- PDF: `<span class="stars-na">n/a</span>` — dim, italic, no glyphs, no accent.
  Verified in `hb_23683001107-1.png`: the Continuity row reads `Continuity | — |
  — | — | n/a` and is visibly *absent* rather than *failed*.
- JSON: `{"score": null, "stars": null, "display": "n/a", "note": "<the metric's
  own reason>"}`. Never `0.0`. The metric's existing note (`not enough to grade`
  vs `no splits recorded`) is what distinguishes an abstention from a thin pool
  and must ride along — `build_prompt` already prefers it over a generic phrase.
- hero, nothing graded at all: `not graded` in dim italic, no star row.

**The capped state.** Today: `Overall: capped at C — a metric graded F.` The
letter is named, which a star card cannot do. Replacement:

```
Overall: held to ★★★☆☆ 3.00 — Pace missed badly (uncapped 4.10).
```

Both numbers, so the note reconciles by arithmetic — the same contract the HR
row keeps (`actual − expected = delta`). A reader must be able to see *what the
score would have been* and *which metric took it away*; a bare "capped" states a
penalty without its cause, and the cause is the whole point of the cap.

Note for whoever owns the score: a hard cap on a **continuous** scale is a
discontinuity the display cannot soften — a 4.99 and a 4.10 both land on exactly
3.00 and the star row shows them as identical. That is a scoring decision, not a
display one, but it is worth deciding deliberately rather than inheriting.

---

## 5. THE SEVERITY VOCABULARY

Current `_GRADE_SEVERITY` keys on the base letter: `A → "on target"`, `B →
"slightly off target"`, `C → "off target"`, `D → "well off target"`, `F →
"missed badly"`, else `"n/a"`.

Replacement, `star_severity(score)`:

| score | word |
|---|---|
| ≥ 4.90 | `dead on` |
| ≥ 4.50 | `on target` |
| ≥ 3.50 | `slightly off target` |
| ≥ 2.50 | `off target` |
| ≥ 1.50 | `well off target` |
| < 1.50 | `missed badly` |
| `None` | `n/a` |

The lower five cut points (4.5 / 3.5 / 2.5 / 1.5) reproduce the existing bands
exactly under a linear map (`score = 1 + GPA`), so the read's tone does not
silently move when the scale does.

**The `≥ 4.90` band is new and I recommend adding it.** Today A+ is 63% of
distance rows and 90% of HR rows, and `A+`, `A` and `A-` all collapse to
`"on target"` — on most cards the model is handed one word for every metric and
has nothing to write four different paragraphs about. `dead on` separates
"exactly what was prescribed" from "comfortably inside the band", which is a
real distinction the reader can act on and the only new information this change
can give the prose.

Properties, checked against the brief's requirements:

- **Monotone**: strictly ordered, one word per band, no ties.
- **Not nameable as a score**: none contains a digit, a letter grade, the word
  "star", a fraction, or a rank. `dead on` is already in the coach's live
  vocabulary (2026-07-23 card: "8.01 vs 8.00 prescribed — dead on"), so it adds
  no new token to the model's context.
- **Not the output vocabulary**: `_GRADE_TONE` must keep saying these are facts
  to communicate, not phrases to reuse, or four paragraphs will all open with
  "dead on".

`tests/test_workout_coach.py::test_grade_severity_keys_on_the_base_letter`
becomes a band-boundary test: assert 4.90/4.89, 4.50/4.49, 3.50/3.49, 2.50/2.49,
1.50/1.49 land on either side, and that `None` and an out-of-range value both
give `n/a`.

---

## 6. WHAT THE PROSE MAY SAY

The rule is unchanged in substance: **the read phrases the finding, it never
states the score.** The score is printed in the table immediately below it and
in the hero immediately above it.

**The prompt must not name a value, and must not name an example.** This is the
0.28.1 finding applied: the previous `_GRADE_TONE` spelled out `"A"`, `"B-"`,
`"C+"` in the same breath as the ban and leaked at 3.1%. Rewritten:

```
The verdicts are already decided and are not yours to revise — do not argue with
them, soften them, or re-grade the run.

Write about the NUMBERS, never about a score. The report card prints a star
rating for each area in the table directly below you, so restating it in your
paragraph is a word spent repeating what the reader can already see. Do not name
it, do not count it, do not restate it as a number or a fraction, and do not
invent a scale of your own. Make the reason obvious instead: what he was held
to, what he actually did, and whether that gap matters.
```

The concept is named once (`a star rating`) because the model needs to know what
it must not repeat; **no value ever appears** — `build_prompt` continues to hand
it `star_severity(...)` in place of the score, exactly as it does today.

**Detection.** Keep `_GRADE_LEAK` (a letter is now an *invented* scale, which is
also banned) and add a star pattern beside it:

```python
_STAR_LEAK = re.compile(
    r"(?:^|(?<=[\s(]))(?:"
    r"[★☆¼½¾]"                                          # a glyph is never prose
    r"|(?:\d(?:\.\d+)?|one|two|three|four|five|half)"
    r"\s*(?:and\s+(?:a\s+)?(?:half|quarter)\s+)?stars?\b"
    r"|stars?\s+(?:rating|score)\b"
    r"|\d(?:\.\d+)?\s*(?:/|out\s+of)\s*(?:5|five)\b"
    r")", re.I)
```

**A star leak is far more detectable than a letter leak was, and the false
positive is far less likely.** The bare-`A` problem was that `A` is the English
indefinite article — the single most common word on the page. `stars` is not a
word a running read has any reason to contain. The pattern is still narrowed by
construction rather than matching a bare `\bstars?\b`, for one known lookalike:
**"you were seeing stars by mile six"** is plausible coach prose, and requiring a
count before the word or `rating`/`score` after it lets that through clean.

**Do NOT try to catch a bare numeral.** `4.75` is indistinguishable from a
distance, and the read legitimately writes `5.01 vs 5.00 prescribed`, `TE 3.1/2.1`
and `2.5`. A numeric rule would fire on correct prose constantly, and a false
positive throws away a clean read and buys another generation. The structural
defence is that the number is not in the prompt to echo.

`find_grade_leak` keeps its signature and returns the first offending token from
either pattern, so `reflect.py`'s existing call site and the ONE regeneration in
`generate_read_cached` are unchanged. Extend **both** lists in
`tests/test_workout_coach.py` (real leaks + lookalikes) before touching either
pattern — `seeing stars`, `starting pace`, `a star performance` (leak? no — no
count, no rating word: passes, and that is the deliberate call) go in the
lookalike list.

---

## 7. ACCESSIBILITY / HONESTY

A star row carries an unavoidable review-score connotation, and for this card it
**misleads** — but the fix is a sentence, not a different mark. Amazon and Yelp
stars answer *how good was it*; this card answers *did you do what the day
prescribed*, and this module already has scar tissue proving those are different
questions. The 0.40.0 compliance/stimulus split exists because grading training
load alongside HR made a **perfectly executed easy run** score a C while the run
that blew its HR cap from mile three scored an A — the rubric was inverted
precisely because "followed the plan" and "was a great workout" had been
conflated. A five-star easy day is *by design* a low-stimulus day; a five-star
day can be a slow shuffle if slow was what was written. So the card must state
its own claim, on every surface, once: **"5 stars = you did what the day
prescribed. It is a compliance score, not a verdict on how good the run was."**
It rides the hero in the PDF (dim serif italic, under the meta line), the line
under `## Overall` in the markdown, and a `scale_note` field in the JSON so an
agent reading the card aloud cannot drop it. On the mechanical side the star row
is an `<svg role="img" aria-label="4.75 out of 5">` — the glyphs are drawn paths
and carry no text layer at all, so without the label a PDF reader gets nothing —
and the numeral is always printed beside the stars, in every surface, so no
reader ever depends on counting shapes.

---

## 8. The stored/query surface (`list_report_cards` / `get_report_card`)

Same single-source discipline as `metric_table` / `split_table` /
`stimulus_rows`: **one pure `report_card.star_display(score)`**, consumed by the
markdown renderer, the PDF and the JSON payload. Two renderers of a rating that
can disagree is the exact failure the already-built-card contract exists to
prevent.

Per metric and for the overall:

```json
{"score": 4.75, "out_of": 5, "stars": "★★★★¾", "display": "★★★★¾ 4.75 / 5",
 "severity": "on target", "capped_to": null}
```

`display` exists so an agent renders it verbatim rather than re-deriving a star
string of its own — the same reason `workout_report_card` returns a preformatted
`markdown` card. `severity` is included because it is what the *prose* was
allowed to say, and an agent summarising a card aloud should be held to the same
contract as the coach read. `scale_note` sits at the card root.

**Legacy rows.** All 16 stored cards carry letters
(`metrics[k]["grade"] == "A+"`, `overall.grade == "A"`, `gpa`) and
`get_report_card` hands stored `card_json` straight back. `star_display` must
therefore tolerate a card with no score — return the stored letter unchanged
rather than raising, the same defensiveness `metric_table` already applies with
`.get` for pre-`continuity` cards. If instead the stored rows are to be
converted, that is a `card_store.migrate_*`-shaped job run from
`db.init_schema` (idempotent, rewriting `card_json` and **nothing** else — never
`graded_at`, never the read cache key), and it must not change
`read_cache_key`, or every stored card regenerates its read at 14.5 s each.
Either way: after this ships, `uv run python scripts/warm_report_cards.py`
(survey, then `--yes`) — the read prompt changes, so all 16 keys invalidate.

---

## Calibration gate output at release

```
Report-card calibration — 42 running efforts, trailing 90 days

occupancy strip: 17 quarter-star buckets, 1.00 (left) -> 5.00 (right)

metric                 1.00 .. 5.00        mean    n  verdict
-------------------------------------------------------------
distance               ##.#...#..#.#.###   4.43   42  ok — 9 buckets used, top 74%, interior 24%, <=2.0* 12%
pace                   #.#...##.##.#####   4.19   42  ok — 11 buckets used, top 45%, interior 52%, <=2.0* 5%
hr (rolling band)      ....#......##.###   4.71   31  ok — 6 buckets used, top 77%, interior 23%, <=2.0* 6%
hr (prescribed cap)    #.#..##.#.....#.#   2.88   11  ok — 7 buckets used, top 27%, interior 45%, <=2.0* 36%
continuity             #.#...##....#..##   4.29   39  ok — 7 buckets used, top 74%, interior 18%, <=2.0* 13%

overall (informational, not gated)
  ...#....#########  mean 4.14, median 4.48
  10/42 at 5.00 (24%)
  cap fired on 12/42 cards (29%)

OK — every rated metric still uses its scale.
```
