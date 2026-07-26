# `get_report_card`

> One stored workout report card by `activity_id` — the full graded snapshot, the coach's verbal read from that render, and a preformatted markdown card. **Availability:** stdio + HTTP

## What it does

Loads one row from `report_cards` and hands back the whole card: the four graded
metrics with their references and expectations, the overall grade, the intent,
the splits as rendered, and the four-paragraph coach's read from that render.

**Render the `markdown` field to the user VERBATIM.** It is already the formatted
card — the same rendering the PDF is built from. Re-summarizing it into your own
verdict is exactly the failure this tool exists to prevent: the grades are
deterministic Python and the read is the coach's phrasing of them, so a
paraphrase can only drift out of agreement with the letters in the table.

Use [`list_report_cards`](list_report_cards.md) to find `activity_id`s. Use
[`workout_report_card`](workout_report_card.md) (stdio-only) when you want a
*fresh* grading of a session, or a PDF.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_id` | integer | yes | — | From [`list_report_cards`](list_report_cards.md) or [`query_workouts`](query_workouts.md). Must be an `int` — a numeric string is rejected. |

## Returns

```json
{
  "activity_id": 23685126977,
  "date": "2026-07-21",
  "graded_at": "2026-07-21T18:04:11",
  "card": {
    "activity": {"activity_id": 23685126977, "date": "2026-07-21", "activity_type": "running", "…": "…"},
    "intent": "interval session",
    "intent_source": "plan",
    "intent_class": "quality",
    "reference": {"…": "…"},
    "plan_workout": {"…": "…"},
    "metrics": {
      "distance": {"grade": "D-", "actual": 9.2, "expected": 5.95, "…": "…"},
      "pace":     {"grade": "A",  "…": "…"},
      "hr":       {"grade": "B+", "expected_display": "134–146", "band": [134, 146], "in_band": false},
      "load":     {"grade": "A",  "spike": true}
    },
    "overall": {"grade": "B+", "gpa": 3.33, "graded_metrics": 4},
    "splits": {"available": true, "unit": "Mile", "rows": ["…"], "hr_drift_pct": 4.1},
    "coach_read": {
      "distance": "…", "pace": "…", "hr": "…", "load": "…"
    },
    "context": {"…": "…"}
  },
  "markdown": "## Report card — Jul 21 …",
  "coach_read": {"distance": "…", "pace": "…", "hr": "…", "load": "…"}
}
```

| Key | Meaning |
|---|---|
| `activity_id` | Echoed from the stored row. |
| `date` | The workout's date (`activity_date`). |
| `graded_at` | When this snapshot was written — see the gotcha; it is not "when you last looked". |
| `card` | The full stored card dict. |
| `markdown` | The preformatted card. **Render verbatim.** `null` when rendering failed (see gotchas). |
| `coach_read` | Convenience copy of `card["coach_read"]` — the four labelled paragraphs (`distance`, `pace`, `hr`, `load`), or `null` on a row with no read. |

Failure returns `is_error: true`, with the id echoed so the caller can recover:

```json
{"error": "no stored report card for activity 23685126977 yet — a card is stored whenever it is rendered from a local session (workout_report_card is stdio-only and cannot be called over the network)",
 "activity_id": 23685126977}
```

## Example

> "Remind me how that interval session on the 21st went."

```json
{"activity_id": 23685126977}
```

Then paste the returned `markdown` into the reply unchanged, and add coaching on
top of it rather than instead of it.

## Gotchas

- **`markdown` can be `null` and the call still succeeds.** A snapshot stored
  before a renderer change may fail to re-render; the structured `card` is the
  data and the markdown is sugar, so the tool logs a warning and returns the
  card anyway. Fall back to reading `card` fields directly — never report the
  card as missing.
- **This is a snapshot, not a live regrade.** The grades reflect the plan that
  was active at that render. If the plan has changed since, a fresh
  [`workout_report_card`](workout_report_card.md) can legitimately return a
  different letter for the same run. Neither is wrong; they answer different
  questions. Say which one you're quoting.
- **`graded_at` lags on purpose.** The save is one atomic guarded UPSERT keyed
  on the read's prompt key — an equal-key re-render is a byte-identical no-op,
  so re-viewing a card does not move `graded_at`. It only moves when the card's
  inputs actually changed.
- **The card's words and grades always come from ONE render.** A
  template-fallback render (the deterministic read used when the SDK call fails)
  never overwrites a row that holds a real generated read, and no splicing path
  exists. So `coach_read` can be trusted to describe the grades printed beside
  it — which is why re-summarizing it is a downgrade.
- **Three keys are stripped at save and re-defaulted to `[]` on load:**
  `hr_trace`, `recent_activities`, `upcoming_workouts`. They are
  presentation/prompt-only (no grade reads them) and reproducible from the DB,
  so they are absent from a stored card by design — an empty `hr_trace` here
  does not mean the run had no HR data.
- **A missing card means never rendered, not badly run.** There is no backfill;
  history starts when cards start rendering. And `workout_report_card` is
  stdio-only, so a phone connected over `/mcp/` can read cards but cannot create
  the one it's asking for.
- **The stored read doubles as the render cache.** A re-render whose prompt key
  matches this row reuses the read with no SDK call — which is why the read here
  is usually identical to what a fresh local render would print.

## See also

- [`list_report_cards`](list_report_cards.md) — find `activity_id`s and trend the grades
- [`workout_report_card`](workout_report_card.md) — render fresh (and store); stdio-only, returns a PDF path
- [`get_workout_detail`](get_workout_detail.md) — the raw session; use the card when a graded one exists
- [`list_coach_memories`](list_coach_memories.md) — the *other* durable record: the coach's journal
