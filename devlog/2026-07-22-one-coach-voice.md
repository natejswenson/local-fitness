# 2026-07-22 — One coach voice, and the prompt that was overriding it

## Why

Nate asked to make sure his voice profile is applied consistently across the MCP
tools. The audit's first answer was reassuring and wrong-ish: the profile *is*
resolved and injected at all three LLM call sites and into the MCP server
`instructions`. `persona` and `dials_line` were present everywhere.

The drift was in what surrounded them.

## What was actually inconsistent

| | brief / chat | PDF coaching line | report-card read |
|---|---|---|---|
| `user_name` | from settings | **hardcoded "Nate"** | **hardcoded "Nate"** |
| profile heading | yes | **no** | **no** |
| "notes REFINE the profile" | yes | **no** | **no** |
| persona + dials | yes | yes | yes |

Plus `brief_planner` defaulted `user_name` to `"Nate"` where everything else
defaulted to `"the user"`, and there was no `config.user_name()` at all — so the
display name skipped the DB > env > default chain that `coach_profile` gets.

The hardcoded name is two bugs in one: a changed setting was ignored on both PDF
surfaces, and a stranger cloning the public repo was told it was Nate's coach.

## The one the gate found

Writing the enumerating test turned up something the audit had missed. The
brief's **steps** mandate is correctly gated on `profile.includes_harsh_block`.
The **conditioning** mandate was not — it hardcoded:

> Override the soft coach voice. Be harsh. {user_name} explicitly asked to be
> motivated to work out…

unconditionally. Selecting `supportive` or `neutral` still produced a roast the
moment fitness slid. A profile that the prompt carrying it can override is not a
profile.

It now has a profile-deferring twin that keeps every fact — the CTL slide, the
training gap, one concrete session — and drops the override. Measured
consequence: only `supportive` and `neutral` render differently. `adaptive` and
`hardass` are byte-identical, so Nate's live brief is untouched.

## How the refactor stayed safe

The three brief/chat prompts were refactored onto shared blocks and verified
**byte-identical across all four profiles** before anything else changed — 12
renderings captured before, diffed after, zero drift. That is what let this ship
without owing the brief an A/B: the prompt genuinely did not change.

The two coach prompts DID change, so they got the measured A/B the report card's
rules require (5 generations per arm on activity 23685126977):

| | `parse_read` fail | median latency | over-budget paragraphs | grade leaks |
|---|---|---|---|---|
| old | 0/5 | 9.3s | 7/20 | 2/20 |
| new | 0/5 | 9.2s | 4/20 | 3/20 |

Neutral on latency, better on the word budget, four-section contract intact.

## Known, pre-existing, not fixed here

The read leaks a letter grade in roughly 10-15% of paragraphs despite
`_GRADE_TONE` forbidding it outright ("PACE: F. Target 6:58/mi…"). It shows up
in **both** A/B arms, so it predates this change. Fixing it is another prompt
edit and another A/B; filed rather than smuggled in.

## The actual deliverable

Not the fixes — the gate. `tests/test_prompts.py` now enumerates every
voice-bearing surface × every profile and asserts each carries that profile's
persona, dials, name, the configured user name, and the notes-precedence rule.
Plus an `ast`-based guard (not a grep — docstrings may name him, string
literals that reach a model may not) that fails the build on a hardcoded
personal name in any prompt module.

Adding a prompt surface now means adding it to `_voice_surfaces`. That is the
part that keeps this true next month.

## Verification

`uv run pytest` (1445 passed, 94.65%), `ruff`, `scripts/score_prompt.py` 11/11,
byte-identity diff, and the A/B above.
