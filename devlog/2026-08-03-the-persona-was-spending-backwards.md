# 2026-08-03 — the coach was briefed on everything except its actual job

0.45.0. The second finding from auditing my own usage rather than my own code.

The persona — the ~15,000-character system prompt that defines the coach — is
delivered on every `/coach` invocation. It is the single largest fixed cost of
talking to this thing. So I did something I should have done a year ago: split
it into sections, and put each section next to the number of times its tools
were actually called.

| persona section | tokens | tools it orients toward | real calls |
|---|---|---|---|
| Managing preferences conversationally | **532** | `save`/`update`/`delete_user_note` | **0** |
| Writing your journal | **199** | `save_coach_memory` | **0** |
| Remembering past conversations | **149** | `recall_coach_memories` | **0** |
| Your graded workout history | 212 | `list_report_cards` / `get_report_card` | 3 / 2 |
| Formatting your chat replies | 370 | `chart` | 4 |
| **— no section exists —** | **0** | **every plan tool** | **62 of 247** |

880 tokens instructing the coach on how to use tools it has never once called.
Nothing at all about the thing I do more than anything else.

## They're superseded, not unlucky

A zero could just mean "rare but important." I checked whether these were
dormant or dead:

- `data/user_notes.md` — last written **2026-06-18**. Preferences moved to
  `update_coach_personality`, which has 25 calls. The 532-token section is
  teaching a workflow I stopped using six weeks ago.
- `coach_journal` — 18 entries: **14 `report_card`, 4 `brief`, 0 from chat.**
  The journal is genuinely populated, but entirely by `agent/reflect.py`
  running automatically. The 199 tokens telling the coach when to write one by
  hand have never produced an entry.

So the instructions aren't wrong. They're just describing a version of the app
that no longer exists, at a cost paid on every single session.

## What went in instead

```
# Managing the training plan
You own the plan — there is no UI, and Nate sees only what you tell him.
...
```

Four things, and the selection criterion matters: each is a constraint that
**spans two tools**, which is precisely what a tool description cannot teach.
A tool description is written from inside one tool. It cannot tell you that
`update_plan_workout` edits one existing day and therefore a swap is *two*
calls. It cannot tell you that a cap written into `description` prose is
invisible to the grader, so you must also pass `hr_max`. It cannot tell you
that leaving `pending_draft` open means the next `propose_training_plan`
silently archives it.

Those are seams. Seams belong in the persona.

## The net is negative

```
before:  14,920 chars
after:   12,016 chars   (−2,904, ~726 tokens)
```

The persona got *smaller* while gaining a whole section on the thing I do most.
That's the shape a good reallocation has. If it had grown I'd have been much
less confident it was the right call.

There is now a ceiling test at 13,000 chars. Not because 13,000 is meaningful,
but because the failure mode here is drift: every individual addition to a
prompt is justifiable, and prompts get to 15,000 characters one justifiable
addition at a time. Raising the ceiling is allowed. Sliding past it is not.

## Compression is not free, and the tests said so

I nearly deleted "Writing your journal" outright. `tests/test_prompts.py`
stopped me:

```python
assert "save_coach_memory" in text
assert "recall_coach_memories" in text
assert "session note" in text
assert "Never say you don't remember without searching" in normalized
assert "never cite a memory the search didn't return" in normalized
```

That's the grounding contract for the coach's memory — the rule that stops it
inventing a recollection. Deleting the section would have taken the contract
with it. So the section got compressed to a third of its size with those exact
strings intact, which is a better outcome than the one I was about to ship.

A test that pins *specific prompt strings* looks fussy right up until it saves
you.

## And a small one: the version was a lie

While in `tools.py`:

```python
return create_sdk_mcp_server(name=SERVER_NAME, version="0.6.0", tools=...)
```

Hardcoded in the server's first commit and never touched. Every MCP client —
Claude Desktop, opencode, my phone over `/mcp/` — has been reporting
**`fitness v0.6.0`** while the app shipped 0.44.0. Thirty-eight releases.

That is the exact number you squint at in a client to work out whether the fix
you just deployed is actually the one running. It now reads from installed
package metadata.

The test for it compares against `pyproject.toml` parsed directly, *not*
against `importlib.metadata` — asserting the code's own source against itself
would pass no matter what either one said. Same trap as the CVE-detector test
in 0.43.1, two days running. Worth naming as a pattern: **when you test a value
that's read from somewhere, assert against a different somewhere.**
