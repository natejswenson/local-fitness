# `list_report_cards`

> Past workout report cards as stored snapshots, newest run first — the overall 1-5 star score and the four metric scores per row, without re-rating anything. **Availability:** stdio + HTTP

## What it does

Lists rows from the `report_cards` table: every card
[`workout_report_card`](workout_report_card.md) has rendered, saved at render
time. One call answers "how have my quality days trended", "what did I score on
my last five long runs", "am I getting better at pacing".

Nothing is computed here — no rating, no plan lookup, no SDK call. The scores
come back exactly as they were shown when the card was rendered.

Two tools, two jobs: this one lists many cards shallowly;
[`get_report_card`](get_report_card.md) returns one card in full with the
coach's verbal read and preformatted markdown.

**Availability note worth internalizing:** this tool and
[`get_report_card`](get_report_card.md) are pure JSON, so they work over both
transports — but `workout_report_card`, the thing that *creates* rows, is
stdio-only. A remote `/mcp/` client can read the history it can't extend.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `start_date` | string | no | — | Earliest **workout** date, `YYYY-MM-DD` inclusive. Filters `activity_date`, not `graded_at`. Validated by `date.fromisoformat` + a length-10 check. |
| `end_date` | string | no | — | Latest workout date, inclusive. Same validation. |
| `intent_class` | string | no | all | One of `easy`, `long`, `quality`, `steady`. An unknown value is a hard error that echoes the allowed set. |
| `limit` | integer | no | `20` | Max cards. **Validated 1–500** — a non-int (including `true`), `0`, or `-1` is a hard error. |

## Returns

```json
{
  "cards": [
    {
      "activity_id": 23685126977,
      "date": "2026-07-21",
      "graded_at": "2026-07-21T18:04:11",
      "intent": "interval session",
      "intent_class": "quality",
      "overall": "B+",
      "gpa": 3.33,
      "capped_by": null,
      "stars": {"distance": 1.31, "pace": 5.0, "hr": 3.76, "continuity": null},
      "legacy_grade": null
    }
  ],
  "count": 1,
  "truncated": false
}
```

| Key | Meaning |
|---|---|
| `cards` | Rows sorted `activity_date DESC, activity_id DESC` — newest **run** first, not most recently rated. |
| `count` | `len(cards)`. |
| `truncated` | `true` when more cards matched than `limit` returned. Always present. |

Per card: `activity_id` (the key for [`get_report_card`](get_report_card.md)),
`date` (the workout's date), `graded_at` (when this snapshot was written),
`intent` (the free-text session intent) and `intent_class` (the grading bucket),
`overall` + `gpa`, `capped_by` (`"F"` when an F on one metric capped the overall
at C), and the four metric letters. A metric that couldn't be graded — no
reference pool, no splits on a quality day — carries `"n/a"`.

`truncated` comes from a `limit + 1` fetch, the same pattern as
[`query_workouts`](query_workouts.md).

## Example

> "How have my quality days been going this month?"

```json
{"intent_class": "quality", "start_date": "2026-07-01"}
```

```json
{"cards": [
   {"activity_id": 23685126977, "date": "2026-07-21", "intent_class": "quality",
    "overall": "B+", "gpa": 3.33, "capped_by": null,
    "grades": {"distance": "D-", "pace": "A", "hr": "B+", "load": "A"}},
   {"activity_id": 23412009855, "date": "2026-07-14", "intent_class": "quality",
    "overall": "C", "gpa": 2.0, "capped_by": "F",
    "grades": {"distance": "B", "pace": "F", "hr": "A-", "load": "B+"}}
 ],
 "count": 2,
 "truncated": false}
```

Note the second row: `capped_by: "F"` means the overall was floored at C by the
pace F, not averaged into one.

## Gotchas

- **History accumulates as cards render — there is no backfill.** A workout with
  no row was simply never graded. An empty result for last March is the expected
  state, not missing data, and it does not mean those runs went badly. Say
  "no card was rendered for that run", never "you have no data".
- **These are dated snapshots, not a live view.** Each row was graded against
  the plan active at that render. `build_card` grades against the *currently*
  active plan, so a fresh render today can produce a different letter for the
  same workout. That's drift, and it's labeled rather than hidden — never
  present a stored grade as the definitive verdict if the plan has since
  changed.
- **`graded_at` dates the most recent *distinct-key* render, not the most recent
  view.** The save is one atomic guarded UPSERT keyed on the read's prompt key:
  an equal-key re-render is a byte-identical no-op, so `graded_at` can lag a
  render that happened this morning. It moves only when the card's inputs
  actually changed.
- **Sorting is by `activity_date`, deliberately.** `graded_at` ordering would
  float an old run to the top just because someone re-rendered it. Newest run
  first is the "trend my training" ordering.
- **The date filters are on the workout, not the grading.** `start_date` /
  `end_date` compare against `activity_date`. There is no way to filter by
  `graded_at` here.
- **A `null` grade column is possible on older rows** — a metric that returned
  `n/a` stores `"n/a"`, but a row written before a metric existed can carry
  `null`. Treat both as "not graded".
- **Check `truncated` before saying "that's all of them"** — the default is 20.
- **No delete or prune path exists.** Rows accumulate forever. That is
  load-bearing, not an oversight: `workout_report_card`'s read-cache fast path
  reads outside the UPSERT's guard, so a pruning tool would open a corrupting
  window. Don't ask for one without revisiting that path.

## See also

- [`get_report_card`](get_report_card.md) — one card in full, with the coach's read
- [`workout_report_card`](workout_report_card.md) — render (and thereby store) a card; stdio-only
- [`get_training_plan_progress`](get_training_plan_progress.md) — plan adherence, the *other* "how am I doing" axis
- [`query_workouts`](query_workouts.md) — the raw sessions behind the cards
