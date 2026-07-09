---
ticket: "#TBD"
title: "Model-agnostic alt-model shadow-run (generalize the gemma4/Ollama path via opencode)"
date: "2026-07-08"
source: "design"
---

# Model-agnostic alt-model shadow-run

## Goal

The brief generator's shadow-run comparison path (`agent/briefing.py`'s
`generate_streaming`, driven by `scripts/shadow_run.py`/`scripts/ab_brief.py`)
currently only reaches ONE model: Ollama-hosted `gemma4`, via a bespoke
Ollama-native HTTP client (`agent/local_model.py`) and a string of
`if alt_model_name == "gemma4"` branches in `briefing.py`. The user is
actually using `opencode/deepseek-v4-flash-free` today (a model reachable
only through opencode's own hosted gateway, not a local Ollama daemon) — the
current code cannot reach it at all. This design generalizes the dispatch so
any model `opencode` can reach is usable for shadow-run comparison, without
regressing gemma4's already-validated compliance fix.

## Why this shape (capability-aware dispatch, not one uniform transport)

The original shadow-run design doc measured real numbers: a base prompt got
gemma4 to ~5/12 valid briefs; a gemma4-specific *stricter prompt* was **still**
~5/12 (no improvement); only Ollama's native grammar-constrained decoding
(`format=<json-schema>`) reached **12/12 valid, 0 flakes**. `opencode run` has
no passthrough for that mechanism — confirmed via a live, unresolved upstream
GitHub issue explicitly requesting schema-constrained CLI output, which
doesn't exist yet. Routing gemma4 through opencode's generic CLI would mean
losing a verified 12/12 guarantee for an unverified prompt-only ~5/12 one.

So dispatch is **capability-aware, not uniform**: when the configured model's
provider is `ollama`, dispatch stays on the existing direct-HTTP client with
grammar-constrained decoding (unaffected, no regression). Every other
provider (opencode's own hosted models, and any future non-Ollama provider)
goes through a new opencode-CLI subprocess transport, accepting prompt-only
best-effort compliance — not a regression, since there is no alternative
access path for those models today. This is still genuinely model-agnostic:
no code hardcodes a specific model *name*; the two transports are selected by
provider *capability*, and a user can add any new opencode-reachable model
without touching Python code.

## Dispatch-key convention

One unified prefix replaces `"ollama:"`: `"opencode:"` followed by the exact
`provider/model` string the user would type at `opencode -m` (matching
`opencode models`' own catalog output), e.g.:
- `"opencode:ollama/gemma4"` → direct Ollama HTTP client (bare model `gemma4`)
- `"opencode:opencode/deepseek-v4-flash-free"` → opencode-CLI transport

The user never needs to know which internal transport handles their model —
same string shape either way. `_alt_model_name()` strips the prefix and
returns the full `provider/model` string; callers `.partition("/")` it to get
`provider` (routing decision) and `bare_model` (the literal value passed to
whichever client is chosen).

**Scope-limitation precondition — `"ollama"` here means exactly one thing,
not "whatever opencode's own ollama provider is configured to point at."**
This design's goal statement ("any model opencode can reach") is narrowed for
the entire `ollama/*` provider namespace, and that narrowing must be stated
plainly rather than left implicit: for THIS design, any dispatch key whose
`provider` partitions to `"ollama"` is routed to
`local_model.generate_local_completion`, which hardcodes
`DEFAULT_HOST = "http://localhost:11434"` and is never handed a host
override anywhere in this design. That means `opencode:ollama/<anything>`
does NOT go "through opencode" at all — it bypasses opencode entirely and
talks directly to a local Ollama daemon on the default port. opencode itself
supports configuring an `"ollama"` provider against a remote or custom base
URL in a user's own `opencode.jsonc`; this design ignores that possibility
silently — there is no error, warning, or acknowledgment anywhere in the
dispatch path that a user's actual opencode-configured `ollama` endpoint
might differ from `localhost:11434`. Concretely: for this design, `"ollama"`
always means "the local Ollama daemon on `local_model.DEFAULT_HOST`
(localhost:11434), reached directly, bypassing opencode entirely" —
regardless of what the user's own opencode config's `"ollama"` provider
actually points at. This is a deliberate, accepted scope limitation for v1,
consistent with this repo's lean-v1/harden-reactively philosophy — a
host-override mechanism is intentionally not built here. `local_model.py`'s
existing `DEFAULT_HOST` constant is a plausible future extension point if a
user genuinely needs a non-default Ollama host, but plumbing that through is
out of scope for this design.

**Naming note — "local" is no longer accurate and must not survive the
rename.** The existing code's `_LOCAL_MODEL_PREFIX` constant and
`_local_model_name()` function (in `briefing.py`) are renamed to
`_ALT_MODEL_PREFIX` and `_alt_model_name()` — an "alternate model" framing,
since that's accurate for both the still-genuinely-local Ollama case and the
off-machine opencode-gateway case (see the Threat model note below for why
"local" is actively misleading once opencode-routed models are in scope). The
`local_model_name` local variable in the dispatch code sketch is renamed to
`alt_model_name` for the same reason. For the same reason, the dispatch
branch's other locally-scoped sketch variables (`local_system_prompt`,
`local_prompt_text`, `local_format`) are likewise renamed to
`alt_system_prompt`, `alt_prompt_text`, `alt_format` — these now feed the
opencode (off-machine) transport as well as the Ollama one, so keeping a
"local"-prefixed name on data that can leave the machine would be
misleading, even though this is a code-sketch-only naming cleanup rather
than a change in scope. **`local_temperature` is a deliberate exception to
this rename list, not an omission:** the current code's `local_temperature`
variable isn't renamed to `alt_temperature` — it's eliminated entirely,
folded into `profile.temperature` on the per-model profile dataclass (see
"Per-model profile registry" below), so a reader cross-checking every
`local_`-prefixed identifier against this list shouldn't expect to find it
under a new name. This rename applies everywhere these
identifiers appear, both in this doc and in the implementation: the API
Surface section, the restructured dispatch code sketch, the Testable
invariants, and `scripts/shadow_run.py`'s prefix-constant import. **The one
exception is the `local_model.py` FILENAME itself** — that module is (and
remains) genuinely Ollama-specific and genuinely local
(`http://localhost:11434`, never leaves the machine), so its filename
accurately describes what it does and is not renamed. Only the *generic*,
provider-spanning dispatch identifiers (prefix constant, dispatch function,
dispatch variable) drop "local." `local_model.py`'s own module docstring,
which currently reads "the `ollama:`-prefixed local-model dispatch," should
be updated during implementation to reference the new
`_ALT_MODEL_PREFIX`/`_alt_model_name()` naming so it doesn't describe a
dispatch mechanism by a name that no longer exists.

## Architecture

### Threat model note: opencode transport changes the stakes, not just the mechanism

Ollama-local traffic never leaves the machine (`http://localhost:11434`).
Opencode-routed traffic (e.g. `opencode/deepseek-v4-flash-free`) leaves the
machine and goes to opencode's hosted gateway — a third party. That means
`_assert_fixture_only_data()` is no longer just a localhost guardrail: for
the opencode transport it is the ONLY thing standing between real personal
health/coaching data and an off-machine LLM provider. Its criticality is
materially higher on this path, and the design should say so rather than let
that fact go unstated.

**The gate has a real hole today and this design closes it.** The alt-model
branch's system prompt is built via `prompts.brief_v2_system_prompt(...)`,
which injects USER NOTES via `notes.render_for_prompt()` — and that function
resolves its path through `notes._default_notes_path()` to
`LOCAL_FITNESS_NOTES_PATH` if set, or, if UNSET, the REAL
`<repo>/data/user_notes.md` file. `_assert_fixture_only_data()` as it exists
today (briefing.py:314-335) checks only `db.DEFAULT_DB_PATH` and
`briefs.DEFAULT_BRIEFINGS_DIR` — it never checks the notes path at all. On a
real machine (which has real personal notes on disk), any invocation of the
opencode-transport branch that doesn't go through `scripts/shadow_run.py`'s
`_capture_v2` (which separately, and without this design ever crediting it,
sets `LOCAL_FITNESS_NOTES_PATH` to a tmp path before calling) would ship REAL
personal notes to a third-party LLM provider while the gate still reports
"safe." So this design makes `_assert_fixture_only_data()` also validate that
`notes._default_notes_path()` resolves under the system temp dir, using the
same allow-list check (`.resolve().is_relative_to(tmp_root)`, where
`tmp_root = Path(tempfile.gettempdir()).resolve()` is computed once and
reused for all three paths — never a raw, unresolved `tempfile.gettempdir()`
call in a fresh expression; see the note below on why this distinction is
load-bearing) already applied to the DB path and the briefings dir — see "Small,
explicit extension to `_assert_fixture_only_data()`" below. This is a small,
necessary code change, not a no-op: without it, the "ONLY thing standing
between real personal data and an off-machine LLM provider" claim above would
rest on an unstated, undocumented harness convention (`shadow_run.py`'s
`LOCAL_FITNESS_NOTES_PATH` override) rather than on the gate itself. With the
extension, the gate genuinely covers everything real-data-related that the
alt-model branch touches, and the harness's env-var override becomes a
belt-and-suspenders precaution rather than the only thing holding this
together.

Mitigation (deliberately lightweight — no new security subsystem):
`generate_opencode_completion` contains **two** `subprocess.run` calls — the
fail-closed `opencode agent list` pre-check, and the actual `opencode run`
that sends the prompt. The warning log goes at the **second** one only:
immediately before the `opencode run` subprocess call, AFTER the fail-closed
agent pre-check has already passed — not at function entry, and not before
the pre-check. e.g. `logger.warning("opencode transport: sending prompt to
off-machine provider %s", model)`. Placing it at function entry (before the
pre-check) would be wrong: it would emit a log even on the pre-check-failure
path, contradicting AC#3's claim that no log is emitted anywhere in that
failure path. This line is unconditional at that specific call site within
`generate_opencode_completion` — it is not a runtime `if provider != "ollama"`
check evaluated inside that function; rather, `generate_opencode_completion`
is structurally unreachable for `provider == "ollama"` models by construction
(they're routed to `local_model.py` instead, per the dispatch above), so every
call that reaches the `opencode run` step is, by definition, off-machine, and
the warning fires every time that step is reached. This makes every
off-machine send auditable/visible in logs rather than silent — it doesn't
add a new gate, it just ensures the one gate that matters
(`_assert_fixture_only_data()`) isn't the only trace of what happened. The
Ollama path emits no such line, since `local_model.py` is a separate module
that never leaves the machine and never calls this function.

**Caveat — "non-`ollama` == off-machine" is a conservative approximation,
not a guaranteed fact.** opencode can front genuinely local providers too
(e.g. LM Studio, a local llama.cpp server), not only hosted-gateway models
like `opencode/deepseek-v4-flash-free`. Since this design routes strictly on
`provider == "ollama"` vs. everything else, a hypothetical future
non-Ollama *local* provider would still trip the warning log — a false
alarm, not a missed one. This is deliberately fail-safe rather than
fail-dangerous: the mechanism is willing to over-warn (an unnecessary log
line for a technically-local non-Ollama provider) in exchange for never
under-warning (silently treating a genuinely off-machine send as local).

### Small, explicit extension to `_assert_fixture_only_data()`

The gate (briefing.py:314-335) currently loops over two `(label, path)`
pairs — `db.DEFAULT_DB_PATH` and `briefs.DEFAULT_BRIEFINGS_DIR` — and raises
if either resolves outside the system temp dir. This design adds a third
pair to that same loop: `("notes._default_notes_path()",
notes._default_notes_path())`, checked with the identical
`.resolve().is_relative_to(tmp_root)` test already applied to the other two,
where `tmp_root` is the SAME pre-resolved `Path(tempfile.gettempdir()).resolve()`
variable the existing loop already computes once and reuses for all
`(label, path)` pairs. **This resolved-`tmp_root` requirement is load-bearing,
not stylistic:** on macOS, `tempfile.gettempdir()` returns `/var/folders/...`
while `.resolve()` yields `/private/var/folders/...` (a symlink target) —
these do not string-compare or `relative_to`-match each other, so any new
one-off expression that calls the raw, unresolved `tempfile.gettempdir()`
directly (instead of reusing the shared resolved `tmp_root`) would make the
gate raise on every legitimate tmp path, breaking `shadow_run.py` entirely.
The third pair must reuse the identical `tmp_root` variable the loop already
has for the other two, not introduce a second, differently-computed
comparison target. No new control flow, no new function — it's the same
allow-list pattern, extended to a third path instead of two — but it DOES
require a new
`from .. import notes` import at the top of `briefing.py`: `notes` is not
currently imported there (the only existing occurrence of the word is in an
unrelated comment), so this is a genuinely new import, not a reuse of an
existing one. After this change, the
gate validates all three real-data surfaces the alt-model branch can touch
(DB, saved briefs, user notes) with one unconditional call, still positioned
before either transport fires. This supersedes the "needs no code change"
framing that appeared in earlier drafts of this design: the check's *shape*
(allow-list, not deny-list) doesn't change, but its *coverage* does, and that
coverage gap was real.

### New module: `agent/opencode_model.py`

Mirrors `local_model.py`'s existing shape and error-normalization contract —
same "one RuntimeError shape" promise, new transport:

```python
def generate_opencode_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,             # "provider/model", e.g. "opencode/deepseek-v4-flash-free"
    agent: str,             # required tool-free opencode agent name
    timeout: float = 300.0,
) -> str:
```

- Builds one message = `system_prompt + "\n\n" + user_prompt` (`opencode run`
  has no separate system-prompt flag).
- **Fail-CLOSED pre-check, the primary (and effectively sole) guard against
  unsafe on-host tool execution — two distinct failure outcomes:** before
  ever invoking `opencode run`, calls
  `subprocess.run(["opencode", "agent", "list"], ...)` (or `opencode debug
  agent <name>`, whichever proves more reliably parseable during
  implementation). This pre-check has two independent failure modes that
  must be reported differently, since they point at different remediations:
    - If the `opencode agent list` subprocess call itself fails (non-zero
      exit code, `subprocess.TimeoutExpired`, `FileNotFoundError`, or any
      other subprocess-level error), that means opencode itself failed to
      respond — broken, unauthenticated, or unreachable — NOT that the agent
      is misconfigured. Raise a **distinct** `RuntimeError` describing that
      opencode itself failed (naming the exit code/exception), so the user
      isn't misdirected toward the `.env.example` agent-setup snippet when
      the real problem is opencode itself.
    - Only if the command succeeds (exit 0) **and** the configured `agent`
      name is absent from its output does the "agent not found" error fire —
      raising `RuntimeError` naming the missing agent and pointing at the
      `.env.example` setup snippet.
  This is proactive: it verifies the agent exists rather than inferring
  failure from opencode's own warning text, so it doesn't depend on
  opencode's stderr wording, locale, or stream choice staying stable — but a
  failed pre-check subprocess call must never be conflated with a
  successfully-run pre-check that reports the agent missing.
- Invokes `subprocess.run(["opencode", "run", "-m", model, "--format", "json",
  "--agent", agent, "--", message], capture_output=True, text=True,
  timeout=timeout)` (argv list form — see Invariants). The `--` sentinel
  immediately before `message` is deliberate, not decorative: `message` is
  passed as a bare positional argv element, and its content is the
  coach-persona system prompt concatenated with user-note text (see the
  Threat model note above) — without `--`, a message that happened to start
  with `-` could in principle be misparsed by opencode's own argument parser
  as a flag rather than as the prompt text (an argument-injection risk,
  distinct from the shell-injection risk that argv-list form already rules
  out). In practice the message always begins with the fixed coach-persona
  system prompt, so this is a low-probability hardening rather than a fix
  for an observed failure, but `--` is the standard, cheap way to close it.
  **This ordering is LOAD-BEARING, not stylistic:** `--` conventionally
  terminates option parsing — everything after it is a positional operand,
  never a flag. `-m`, `--format`, and `--agent` MUST all appear in the
  flag region BEFORE `--`, and `--` must be the LAST flag-region element,
  immediately followed only by `message`. Placing `--` any earlier (e.g.
  before `-m`) pushes `-m model`, `--format json`, and `--agent agent` past
  the terminator too, so opencode's argument parser would silently treat
  all three as positional operands instead of flags — `--format json` would
  never actually select JSON output (silently breaking the NDJSON parser
  this module depends on), and `--agent agent` would never actually select
  the configured tool-free agent (silently discarding the fail-closed
  agent-existence pre-check above and falling back to opencode's default,
  tool-enabled `build` agent regardless of what the pre-check verified).
  Get this wrong and the two safety mechanisms this design spent multiple
  sections building — schema-driven parsing and the tool-free-agent
  guarantee — become moot without any visible error.
- Parses stdout as newline-delimited JSON events; concatenates every
  `type == "text"` event's `part.text` in order (there can be more than one
  text-part chunk before `step_finish`) and returns the joined string. This
  NDJSON event schema (`type=="text"`, `part.text`, `step_finish`) is an
  unversioned external contract. What's actually verified vs. what isn't,
  stated precisely to avoid overclaiming: a manual smoke test against
  **opencode CLI 1.17.15**, for one specific invocation, observed these
  exact event type names and field paths — that part is a real, recorded
  observation, not an assumption. What was NOT exhaustively verified is
  whether `--format json` reliably emits true newline-delimited events
  across every invocation and every version, as opposed to sometimes
  emitting a single JSON document (e.g. one top-level array/object), which
  would break a naive "json.loads per line" parser entirely. Recorded here
  so a future opencode upgrade that changes the event shape has a known
  baseline to diff against. **To confirm during implementation:** re-check
  the newline-delimited-vs-single-document framing against the real CLI
  output before relying on it — no speculative fallback parser is
  prescribed here.
- **Secondary output-integrity check — a different property than
  defense-in-depth against execution:** also scans stderr for the literal
  substring `"not found. Falling back to default agent"` — confirmed by
  direct testing that opencode does NOT error on an unknown `--agent` name,
  it warns and silently falls back to the full-tool-access `build` agent
  (which then has live bash/edit access and costs ~30k tokens for a trivial
  reply in testing). **This check cannot prevent the harm it might sound
  like it prevents:** if the fallback happens, the tool-enabled `build`
  agent has ALREADY EXECUTED — with live bash/edit access — by the time
  stderr is scannable. A stderr scan runs strictly after execution, so it
  can only reject the resulting output; it cannot un-execute anything. That
  means the fail-closed pre-check above is the primary, and effectively the
  *only*, guard against unsafe on-host tool execution. This stderr scan
  instead protects a different property — output integrity: it stops
  `generate_opencode_completion` from silently returning a response that
  came from the wrong, tool-enabled agent, so the caller gets a clear
  `RuntimeError` instead of a result it might trust. If the substring is
  present, raise `RuntimeError` naming the missing agent — this catches the
  case where the pre-check's `agent list` output format itself drifts or the
  agent is removed between the pre-check and the run, but, again, only as an
  after-the-fact output-integrity check, not as execution prevention (a
  fragile, unversioned stderr match is fail-open with respect to execution:
  if opencode changes wording, stream, or locale, it silently passes and a
  tool-enabled agent has already run with live bash/edit access by the time
  anyone could tell). In practice the blast radius of an actual fallback is
  bounded — the fixture-only data means the tool-enabled agent only ever
  sees fixture content, and a "write a brief" prompt is unlikely to provoke
  destructive tool calls — but that bound doesn't change what this check can
  and can't prevent, and shouldn't be read as softening the distinction
  above.
- **Residual risk neither tier catches:** both the pre-check and the stderr
  backstop only detect "the configured agent doesn't exist" (missing name,
  fallback to `build`). Neither check verifies the configured agent is
  ACTUALLY tool-free at runtime — e.g. an agent that exists under the right
  name but was configured with `tools.*` left enabled (a typo or drift in the
  user's own `~/.config/opencode/opencode.jsonc`) passes both checks silently.
  That guarantee rests entirely on the user's own config matching the
  documented `.env.example` snippet. This is an accepted, documented residual
  risk, not a runtime-enforced one — adding a runtime tool-disabled
  verification mechanism is out of scope for this design.
- Raises `RuntimeError` (one normalized shape, mirroring `local_model.py`) on:
  `FileNotFoundError` (opencode binary missing), non-zero exit code,
  `subprocess.TimeoutExpired`, empty/no `text` events found, or any line that
  fails `json.loads`.
- **Message-construction constraint — truncation-safe by ordering, not by
  rewriting the reporting pipeline:** per the flake-count-truncation issue
  described in "`scripts/shadow_run.py` must surface the failure message, not
  just a flake count" below, any retained message text upstream is truncated
  to its first line / 200 characters. So the `RuntimeError` message for the
  agent-fallback and pre-check failures (missing agent, opencode failed to
  respond, stderr fallback detected) MUST be constructed as a SHORT,
  single-line message with the actionable pointer (e.g. "see .env.example")
  placed FIRST, not buried after other diagnostic detail — otherwise
  truncation could clip the actionable part before a user ever sees it. This
  is an ordering/length constraint on the existing message text, not a
  reason to redesign the message format.

### Required manual setup (documented in `.env.example`)

A repo-local `opencode.json` is **not** auto-discovered by opencode (tested
directly) — so a lean, tool-free agent can only live in the user's own
personal `~/.config/opencode/opencode.jsonc`, same category as this project's
existing Garmin-credential / Claude-OAuth-token manual setup steps. New env
var `LOCAL_FITNESS_OPENCODE_AGENT` (default `"fitness-brief"`) names it.
`.env.example` documents the exact JSON snippet to add, standalone: a new
`"fitness-brief"` agent entry under the opencode config's `agent` map, with
every `tools.*` key (`bash`, `edit`, `write`, etc. — whichever tool keys
opencode's schema exposes) explicitly set to `false`. The snippet shown in
`.env.example` should be the complete, literal JSON to paste in, not a
pointer to "mirror" the user's existing `fitness` agent block — that's a
different agent, with a different name and a different (tool-enabled)
purpose, and isn't a template to copy from for this tool-free agent.

**Implementation requirement, not optional — `load_dotenv()` is currently
missing from every reachable entry point.** `LOCAL_FITNESS_OPENCODE_AGENT` is
documented above as configurable via `.env`, but only `cli.py` currently
calls `load_dotenv()`. The only reachable end-to-end entry points for this
design (`scripts/shadow_run.py` and `scripts/capture_baseline.py`, per AC#1)
never call it. That means a user who sets a custom agent name in `.env` gets
the hardcoded default `"fitness-brief"` on these scripts instead of their
configured value, and the fail-closed pre-check would then reject their real,
differently-named agent as "not found." This currently "works" only by
coincidence, because `.env.example`'s snippet happens to name the agent
`"fitness-brief"` (the same as the code default) — the env var is effectively
inert on the only reachable path today. `scripts/shadow_run.py` and
`scripts/capture_baseline.py` both need `load_dotenv()` added, mirroring
`cli.py`'s existing pattern, so `LOCAL_FITNESS_OPENCODE_AGENT` (and any other
`.env`-driven config) actually takes effect on these entry points. Note that
the path-depth index differs from `cli.py`: `cli.py` lives at
`src/local_fitness/cli.py` and correctly uses `parents[2]` to reach the repo
root from that depth, but `scripts/shadow_run.py` and
`scripts/capture_baseline.py` live one level shallower (directly under
`scripts/`), so the correct call for these two scripts is
`load_dotenv(Path(__file__).resolve().parents[1] / ".env")` — copying the
literal `parents[2]` from `cli.py` would resolve to a nonexistent path and
`load_dotenv` would fail silently, silently reproducing the inert-env-var bug
this fix exists to close.

### Per-model profile registry (replaces the `if == "gemma4"` branches)

```python
from dataclasses import dataclass
from collections.abc import Callable

@dataclass(frozen=True)
class _ModelProfile:
    temperature: float = 0.4  # ollama-provider only: consumed by generate_local_completion's
                               # temperature= kwarg; generate_opencode_completion has no
                               # temperature parameter and `opencode run` has no CLI-level
                               # temperature flag (confirmed) — this field is a silent no-op
                               # on the opencode transport, by design, not an oversight
    ollama_format_schema: Callable[[], dict] | None = None  # ollama provider only
    plan_prompt_facts: Callable[[dict], str] | None = None
    plan_status_appendix: Callable[[str, dict | None], str] | None = None  # (raw_json, plan_today) -> raw_json
    reshape: Callable[[str], str] | None = None

_DEFAULT_PROFILE = _ModelProfile()
_MODEL_PROFILES: dict[str, _ModelProfile] = {
    "ollama/gemma4": _ModelProfile(
        temperature=0.8,
        ollama_format_schema=_gemma4_format_schema,
        plan_prompt_facts=_gemma4_plan_prompt_facts,
        plan_status_appendix=_append_gemma4_plan_status,
        reshape=_reshape_gemma4_slots,
    ),
}
```

Keyed by the exact `provider/model` string (not the bare Ollama model name,
so `ollama/gemma4` and `ollama/gemma4-agent` are distinct entries — only
`gemma4` was ever empirically validated, so only it gets a profile; a new
model — DeepSeek included — starts at `_DEFAULT_PROFILE` with zero extra
tuning until a real observed failure justifies adding an entry, mirroring how
gemma4's own tuning arose).

Note the registry entry now points `plan_status_appendix` at
`_append_gemma4_plan_status` (the JSON-payload appender, signature
`(raw: str, plan_today: dict | None) -> str`, matching the real callee), not
at the narrower sentence-only
`_gemma4_plan_status_appendix` generator it internally calls — this is what
makes the field genuinely dispatchable from the restructured branch below
instead of leaving a second hardcoded call site. `_gemma4_plan_status_appendix`
still exists as the sentence-generation helper that `_append_gemma4_plan_status`
calls internally; only the *profile-registry* value changes to the
appender's signature.

Module-ordering note: `_MODEL_PROFILES` references `_gemma4_format_schema`,
`_gemma4_plan_prompt_facts`, `_append_gemma4_plan_status`, and
`_reshape_gemma4_slots` by name, so those gemma4 helper functions must be
defined earlier in the module than `_MODEL_PROFILES` itself — the registry
literal can't forward-reference them. `from dataclasses import dataclass`,
`from collections.abc import Callable`, and `from .. import notes` (see the
gate-extension section above) are new imports needed at the top of
`briefing.py` for the dataclass sketch above. The dispatch code sketch below
also calls `opencode_model.generate_opencode_completion`, so `briefing.py`
needs `from . import opencode_model` too — mirroring the existing,
non-lazy `from . import local_model` already at the top of `briefing.py`
(the established convention for this module's sibling-transport imports;
there's no lazy-import pattern to match here).

### `generate_streaming`'s restructured local-model branch

**Confirmed entry-point coverage:** every eval/shadow-run caller —
`scripts/shadow_run.py`, `scripts/ab_brief.py`, and
`scripts/capture_baseline.py` — calls `briefing._generate`, which itself
drains `generate_streaming`. Separately, the production 06:30 path
(`generate_and_save`) does not go through `_generate` at all — it drains
`generate_streaming` directly via its own inner `_run()` helper. Both paths
converge on the same `generate_streaming` function — that function (not the
alt-model branch inside it) is on the production code path too. Production
always passes a Claude model (`DEFAULT_MODEL`), so `_alt_model_name()`
returns `None` for it and it never executes the alt-model branch itself.
The restructured local-model branch below is genuinely reached by every one
of shadow-run's entry points — the three scripts via `_generate`, each
passing an `opencode:...`-prefixed model string that makes
`_alt_model_name()` return non-`None` — but not by production. Verified
during this review; stated explicitly here so a reader doesn't have to
re-trace the call graph.

```python
alt_model_name = _alt_model_name(model)          # "ollama/gemma4" | "opencode/deepseek-v4-flash-free" | None
if alt_model_name is not None:
    if not _brief_v2_enabled():
        raise ValueError(...)                         # same guard, control flow unchanged — message text updated per rename sweep
    brief_context = brief_planner.assemble_brief_context(...)
    provider, _, bare_model = alt_model_name.partition("/")
    if not provider or not bare_model:                # malformed key, e.g. "opencode:gemma4" (no "/")
        raise ValueError(
            f"malformed alt-model string {model!r}: expected "
            f"'opencode:<provider>/<model>' (e.g. 'opencode:ollama/gemma4')"
        )                                              # mirrors the `if not _brief_v2_enabled(): raise ValueError(...)` pattern above
    profile = _MODEL_PROFILES.get(alt_model_name, _DEFAULT_PROFILE)

    alt_system_prompt = prompts.brief_v2_system_prompt(user_name, coach_profile)
    alt_prompt_text = prompts.brief_v2_user_prompt(...)
    if profile.plan_prompt_facts and brief_context.plan_today is not None:
        alt_prompt_text += profile.plan_prompt_facts(brief_context.plan_today)

    _assert_fixture_only_data()                        # now also checks notes._default_notes_path(); still called before either transport

    if provider == "ollama":
        alt_format = (profile.ollama_format_schema() if profile.ollama_format_schema
                      else Brief.model_json_schema())
        raw = (await asyncio.to_thread(
            local_model.generate_local_completion,
            alt_system_prompt, alt_prompt_text, model=bare_model,
            format=alt_format, temperature=profile.temperature,
        )).strip()
    else:
        agent = os.environ.get("LOCAL_FITNESS_OPENCODE_AGENT", "fitness-brief")
        raw = (await asyncio.to_thread(
            opencode_model.generate_opencode_completion,
            alt_system_prompt, alt_prompt_text,
            model=alt_model_name, agent=agent,
        )).strip()

    if profile.reshape:
        raw = profile.reshape(raw)
    if profile.plan_status_appendix and brief_context.plan_today is not None:
        raw = profile.plan_status_appendix(raw, brief_context.plan_today)  # profile-driven, no hardcoded call

    # ...then finalize exactly as today: `async for evt in _finalize_brief(raw, user_name, save, brief_context): yield evt`
```

`_assert_fixture_only_data()` gets the small, explicit extension described
above ("Small, explicit extension to `_assert_fixture_only_data()`") — it now
also validates `notes._default_notes_path()`, in addition to the DB and
briefings-dir paths it already checked — and stays called in the same
position, before either transport fires.

**Breaking rename, no backward-compat shim.** The OLD pre-rename string shape
(`"ollama:gemma4"`) is not just renamed cosmetically — `_alt_model_name()`
only recognizes the new `_ALT_MODEL_PREFIX` (`"opencode:"`), so an old-shaped
string returns `None` from `_alt_model_name()` and silently falls through to
the Claude dispatch path with an unrecognized model name, rather than the
loud `ValueError` above (that check only fires once `alt_model_name` is
non-`None`). This is a deliberate, undocumented-at-runtime breaking change
with no compatibility shim: this is an internal shadow-run-only diagnostic
tool, not a public API, so any caller passing the old `"ollama:..."` shape
(scripts, saved shell history, etc.) must be updated to the new
`"opencode:provider/model"` shape as part of this rewrite.

### Dead-code cleanup (same commit)

`prompts.py`'s `brief_v2_system_prompt_gemma4`/`brief_v2_user_prompt_gemma4`
are confirmed unused in production (the round-2 finding already noted in
`briefing.py`'s comments: prompt-appendix tuning didn't help, only the
schema trick did) — delete them. Their three tests in `test_prompts.py`
(`test_gemma4_system_prompt_is_superset_of_base`,
`test_gemma4_system_prompt_names_observed_failures`,
`test_gemma4_user_prompt_is_superset_of_base`) are deleted in the same
change, not left dangling. The `# --- gemma4-specific V2 prompt
variants ---`-style section-header comment immediately preceding those
three tests in `test_prompts.py` becomes orphaned by the same deletion
and must be deleted alongside them — the same "leaves nothing dangling"
reasoning applied to `prompts.py`'s constants below applies here too.

**The deletion list above is incomplete on its own — two module-level
constants and their explanatory comment go with it.** Confirmed via grep:
`_GEMMA4_SYSTEM_APPENDIX` (`prompts.py:772`) and `_GEMMA4_USER_APPENDIX`
(`prompts.py:790`) have exactly one consumer each —
`brief_v2_system_prompt_gemma4` and `brief_v2_user_prompt_gemma4`
respectively — and nothing else in the codebase references either
constant. Deleting only the two functions would leave both constants,
plus the ~21-line explanatory comment block that precedes them
(`prompts.py:750-769`, with a blank line at 771, which documents the
now-deleted prompt variants), orphaned as dead code that `ruff` does not
flag (it doesn't catch unused module-level globals). This same commit's
deletion list must therefore also include `_GEMMA4_SYSTEM_APPENDIX`,
`_GEMMA4_USER_APPENDIX`, and the `prompts.py:750-769` comment block, so
the cleanup leaves nothing dangling, per this section's own stated goal.

Note: `briefing.py`'s current lines ~487-496 contain a comment referencing
the (to-be-deleted) `prompts.brief_v2_system_prompt_gemma4`/
`user_prompt_gemma4` functions. This comment is subsumed by the branch
replacement described in "`generate_streaming`'s restructured local-model
branch" above — the whole surrounding branch is replaced by this design's
new code, taking the stale comment with it. No separate cleanup action is
needed beyond that branch replacement.

### `scripts/shadow_run.py` dry-run messaging must become per-provider

Confirmed at lines 252-267 of that file: the current dry-run path prints a
hardcoded `"cost: $0 (local via Ollama)"` / `"Runs locally via Ollama — no
subscription/API cost."` for ANY `alt_model_name`-prefixed model. Under
this design that's factually wrong for opencode-gateway models — an
opencode-routed model (e.g. a non-free DeepSeek variant) is not local and not
necessarily free; it's a network call to a third-party gateway whose cost
depends on the specific model. This is a regression of a previously-fixed
bug (the messaging was already corrected once to stop mis-describing cost).
Fix: branch the dry-run messaging on the resolved `provider` (from
`alt_model_name.partition("/")`), the same way the dispatch itself
branches — `provider == "ollama"` prints the existing local/free message;
any other provider prints something like `"cost: network call via opencode
gateway (<provider>/<model>) — cost depends on the model, not necessarily
free"` and `"Not local — leaves the machine via opencode's hosted
gateway."` (same conservative non-`ollama`-means-off-machine approximation
as the warning-log mechanism — see the caveat in the Threat model note
above). This is called out explicitly so it isn't missed as an
implementation detail; see also Testing strategy below.

Concretely, `shadow_run.py` today only has a `.startswith("ollama:")`-style
check — no partition/strip logic exists there yet — so the implementer
needs the actual derivation, not just "branch on `provider`": strip the
shared `_ALT_MODEL_PREFIX` (see the Testable-invariants bullet on importing
that constant) and partition, e.g.
`provider = args.model[len(_ALT_MODEL_PREFIX):].partition("/")[0]`, then
branch the dry-run message on `provider == "ollama"` exactly as above. Also
note that `shadow_run.py`'s existing `is_local_model` variable name becomes
a misnomer once the prefix changes to `opencode:` (it's no longer testing
"is this a local model," it's testing "is this an alt-model dispatch at
all") — rename it too, e.g. to `is_alt_model`, consistent with this
design's `local_*` → `alt_*` renames elsewhere.

### `scripts/shadow_run.py` must surface the failure message, not just a flake count

**AC#3 below promises the RuntimeError from the fail-closed pre-check "fails
loudly with a clear, actionable" message — tracing the actual failure path
through the entry points named in AC#1 shows that promise is only half
delivered today, and worse than it first appears.** The RuntimeError raised
by `generate_opencode_completion`'s pre-check (or by
`generate_local_completion`, or by the alt-model branch's own dispatch code)
does propagate out of `generate_streaming` uncaught — but **not** because it's
logged and re-raised. The alt-model dispatch branch (`generate_streaming`,
currently ~lines 452-519) `return`s before ever reaching the `try:` block
(~line 599) that wraps the Claude `query()` loop; the module's only
`LOG.exception` call (~line 695) sits inside that block's
`except BaseException` (~line 693), which the alt-model branch never enters.
So today an alt-model RuntimeError propagates completely uncaught — through
`generate_streaming`, through `_generate` (no try/except there either), all
the way to whichever script called it — **with no log emitted anywhere in
that path**, not even to stderr. `scripts/shadow_run.py`'s `_capture_v2`
catches it as `except Exception as e: results.append({"error": str(e)})`, and
`capture_baseline.py`'s `aggregate_scenario` truncates any retained message to
its first line / 200 characters, and `shadow_run.py`'s `parity_report`
ultimately reduces the whole thing to a bare **flake count**
(`flakes=N` / `OVERALL: PARITY FAILED`). So a user running the documented
shadow-run command against a misconfigured agent sees only the flake count in
the actual printed report they read — and the actionable `.env.example`
remediation pointer doesn't survive anywhere else either: there is no stderr
log to fall back on. `_capture_v2`'s per-scenario `print` (below) is not
"surfacing something already logged elsewhere" — it is the **only** place
this message will ever be visible to anyone, which makes it more important to
land, not less.

**Minimal fix required (not a reporting-pipeline rewrite):**
`scripts/shadow_run.py`'s `_capture_v2` should print or retain at least the
first line of the caught exception's message alongside its existing
per-scenario status line — at minimum for alt-model scenarios — instead of
only incrementing a flake count. Concretely: where `_capture_v2` currently
does `results.append({"error": str(e)})` and moves on, it should also print
(or otherwise surface in the interactive output) that first line, e.g.
`print(f"  FAILED: {str(e).splitlines()[0]}")`, so the remediation pointer
(see finding below on message ordering) actually reaches the user reading the
report, not just the logs. This is a small, targeted addition to
`_capture_v2`'s existing per-scenario handling — it does not require changing
`aggregate_scenario`'s truncation or `parity_report`'s flake-count summary.

**Accepted gap — `capture_baseline.py` console output is not fixed to match.**
`capture_baseline.py` is the other entry point named in AC#1, and its
`_capture_live` routes alt-model `RuntimeError`s through the same
`_generate_one` → `{"error": str(e)}` → `aggregate_scenario` truncation
path, but its `main()` prints only a bare flake count to the terminal — the
same message-surfacing gap `_capture_v2` has above, unfixed here. This design
deliberately does not extend the one-line message-surfacing fix to
`capture_baseline.py` for lean-v1 scope: the message still survives in the
written baseline JSON file (which `aggregate_scenario` retains, truncated but
present), and anyone running `capture_baseline.py` against an alt-model is
already required by AC#1's `--out` warning above to treat the run as a
deliberate, attended alt-model capture rather than routine use. If this proves
insufficient in practice, apply the same one-line `print` treatment to
`capture_baseline.py`'s console output as a follow-up.

### Rename sweep: residual "ollama:" string literals outside the main path

The prefix rename (`"ollama:"` → `"opencode:"`) must be applied everywhere
the literal string appears, not just in `_alt_model_name()` and its callers.
**The examples below are illustrative, found during review — they are NOT an
exhaustive list, and must not be treated as the complete set of work.**
Confirmed occurrences include: a `ValueError` message in `briefing.py`'s
local-model branch (currently reads `'local models ("ollama:...") are only
supported...'`), a comment block referencing the old prefix, a docstring in
`test_local_model.py`, `local_model.py`'s own module docstring (which
mentions the `"ollama:"`-prefixed dispatch — see the Naming note above), the
`_ALT_MODEL_PREFIX` (formerly `_LOCAL_MODEL_PREFIX`) constant's own literal
value at its definition site (`"ollama:"` → `"opencode:"`), and roughly a
dozen further literal occurrences across `tests/test_briefing.py`
(spot-checked around lines 617, 629-630, 660, 673, 694, 719, 738, 759, 764,
858, 966, and 993 during this review — line numbers will drift as the file
changes and shouldn't be treated as a checklist). The implementer's actual
obligation is not to work through this list and call it done: run
`grep -rn '"ollama:"'` (and check for non-double-quoted variants too) across
the **whole repo**, not just the files named here, and update every hit,
including error messages, docstrings, and comments — a stale error message
that still tells the user to type `"ollama:..."` would silently break the
documented setup flow.

## API Surface

- `agent/opencode_model.py::generate_opencode_completion(system_prompt: str, user_prompt: str, *, model: str, agent: str, timeout: float = 300.0) -> str` — raises `RuntimeError` on any failure (binary missing, non-zero exit, timeout, malformed/empty output, or detected unsafe agent-fallback).
- `agent/briefing.py::_alt_model_name(model: str) -> str | None` (renamed from `_local_model_name`, backed by a renamed `_ALT_MODEL_PREFIX` constant — see the Naming note in "Dispatch-key convention") — now strips `"opencode:"` (was `"ollama:"`) and returns the full `provider/model` string unchanged (was: bare Ollama model name).
- `agent/briefing.py::_MODEL_PROFILES: dict[str, _ModelProfile]` and `_DEFAULT_PROFILE: _ModelProfile` — new module-level registry, additive-only for future models.
- `local_model.py::generate_local_completion(...)` — **unchanged** signature and behavior; still called for `provider == "ollama"`, now with a bare (unprefixed) model name.
- New env var `LOCAL_FITNESS_OPENCODE_AGENT` (default `"fitness-brief"`).

## Invariants

**Checkable (by inspection):**
- `_assert_fixture_only_data()` is called before either transport, unconditionally, for every non-Claude model, and now checks THREE paths — `db.DEFAULT_DB_PATH`, `briefs.DEFAULT_BRIEFINGS_DIR`, and `notes._default_notes_path()` — instead of two.
- No hardcoded model-name string equality remains in `briefing.py`'s dispatch or transport-selection logic — only the profile-registry lookup and the `provider == "ollama"` capability check.
- `agent/opencode_model.py` never imports or calls anything from `local_model.py` and vice versa — the two transports are independent, swappable per-provider.
- `LOCAL_ONLY`/production 06:30 job code path (`generate_and_save`'s default `model=DEFAULT_MODEL`) never reaches the local-model branch — `DEFAULT_MODEL` is Claude, unchanged.
- Every `subprocess.run` call in `agent/opencode_model.py` (both the pre-check
  and the `opencode run` invocation) uses the list/argv form — never
  `shell=True`. The prompt text passed to `opencode run` can contain
  free-form user-note content (via `plan_prompt_facts`/prior note injection),
  so this is a command-injection guardrail per CLAUDE.md's existing SQL/path
  injection rules, applied to subprocess invocation.

**Testable:**
- A `"opencode:ollama/gemma4"` model string dispatches to `local_model.generate_local_completion` with `model="gemma4"` (bare, unprefixed) and the gemma4 profile's tightened schema/temperature.
- A `"opencode:opencode/deepseek-v4-flash-free"` (or any non-`ollama` provider) model string dispatches to `opencode_model.generate_opencode_completion`, never touches `local_model.py`.
- Setting `LOCAL_FITNESS_OPENCODE_AGENT` to a distinct non-default value (via `monkeypatch.setenv`) and dispatching a non-`ollama` model string results in that exact value — not the hardcoded `"fitness-brief"` default — being passed as the `agent=` argument to a mocked `generate_opencode_completion`, proving the env var actually flows into the call rather than just being read and silently ignored.
- A model string with no matching `_MODEL_PROFILES` entry uses `_DEFAULT_PROFILE` (plain `Brief.model_json_schema()`, temperature 0.4, no plan-facts injection, no reshape) — proving new models get the safe generic path automatically.
- `generate_opencode_completion` raises `RuntimeError` for: non-zero exit, `FileNotFoundError`, `TimeoutExpired`, empty JSON-event stream, malformed JSON line, the fail-closed `opencode agent list` pre-check's subprocess call itself failing (a distinct "opencode failed to respond" error, not "agent not found" — see Architecture), the pre-check succeeding but not finding the configured agent name (the "agent not found, see .env.example" error), and the detected "falling back to default agent" stderr substring (the secondary output-integrity check, not a guard against execution — see Architecture) — each as its own test case, mirroring `test_local_model.py`'s existing per-failure-mode tests.
- **Argv ordering invariant (catches the exact regression this design almost shipped):** a test that inspects the CONSTRUCTED argv list passed to `subprocess.run` for the `opencode run` invocation and asserts, structurally, that `-m`/`model`, `--format`/`json`, and `--agent`/`agent` all appear at indices BEFORE the index of the literal `"--"` element, and that `"--"` is immediately followed by `message` as the final element. This is the only test that can actually catch an ordering regression: since every prescribed unit test here mocks `subprocess.run`, an output-content test can't distinguish "the flag was silently ignored because `--` came too early" from "the flag was correctly parsed and the mocked agent/format value happened to match anyway" — only inspecting the literal list structure (via the mock's `call_args`) proves the ordering itself, not just the mocked return value.
- `scripts/shadow_run.py`'s local-model detection (`args.model.startswith(...)`) imports and uses the shared `_ALT_MODEL_PREFIX` constant instead of re-declaring the literal string (fixes a real coupling gap the Impact Analyst found).
- `scripts/shadow_run.py`'s dry-run messaging prints the ollama local/free message for `provider == "ollama"` and the network/cost-varies message for any other provider — not a single hardcoded "local via Ollama" string for every `alt_model_name`-prefixed model.
- `_assert_fixture_only_data()`'s existing three tests (passes under tmp path, raises for real DB path, raises for real briefings dir) are unchanged and still pass.
- `_assert_fixture_only_data()` raises when `notes._default_notes_path()` resolves outside the temp dir, mirroring the existing DB/briefings-dir checks — a new test case alongside the existing three, proving the gate's notes-path coverage actually works rather than resting solely on `scripts/shadow_run.py`'s `LOCAL_FITNESS_NOTES_PATH` override convention.

## Testing strategy

- `tests/test_opencode_model.py` (new) — mirrors `tests/test_local_model.py`'s structure, using `monkeypatch.setattr(opencode_model.subprocess, "run", ...)` (the existing convention from `tests/test_cli.py`, not a global `subprocess.run` patch) to feed fake `CompletedProcess` objects for each success/failure case, including the new fail-closed `opencode agent list` pre-check's two distinct failure branches (pre-check subprocess itself failing vs. pre-check succeeding but agent missing) and the existing stderr-substring output-integrity check.
- `tests/test_briefing.py` — every existing `"ollama:gemma4"`/`"ollama:llama3.1:8b"` literal is renamed to the new `"opencode:ollama/..."` shape; `test_local_model_name_prefix_parsing` (renamed `test_alt_model_name_prefix_parsing`) gets new cases covering a non-Ollama provider string; a new test proves the profile-registry lookup (gemma4 gets its profile, an unlisted model gets `_DEFAULT_PROFILE`); a new test proves a non-Ollama dispatch never touches `local_model.py` (monkeypatch it to fail loudly if called); a new test proves the profile-driven `plan_status_appendix` call is what's invoked (not a hardcoded `_append_gemma4_plan_status` call) by swapping the registry entry for a stub in the test and confirming the stub fires; a new test (via `caplog`) proves the off-machine warning log line does NOT fire for a `provider == "ollama"` dispatch — this belongs here rather than in `tests/test_opencode_model.py` because it's the dispatch *branching decision* in `briefing.py` that determines whether `opencode_model.generate_opencode_completion` (and its warning log) is reached at all; `generate_opencode_completion` is structurally never called for Ollama-provider models (they go through `local_model.py` instead), so asserting the log's absence inside `test_opencode_model.py` would be vacuous — it can't fire there regardless of whether the code is right or wrong.
- `tests/test_local_model.py` — unchanged except the calling convention in tests that exercise it via `briefing.py` (bare model name, not prefixed).
- `tests/test_prompts.py` — the 3 gemma4-prompt tests are deleted along with the functions.
- `tests/evals/test_shadow_run.py` — a case per provider branch of the dry-run cost/locality message (ollama vs. opencode-gateway), so the messaging fix (see Architecture, "dry-run messaging must become per-provider") doesn't silently regress again.
- `tests/test_opencode_model.py` — a case confirming the off-machine warning log line fires (via `caplog`) when the provider isn't `ollama` (see Architecture, "Threat model note"). The complementary "does NOT fire on the Ollama path" assertion lives in `tests/test_briefing.py` instead (see that bullet above) — `generate_opencode_completion` is structurally unreachable for Ollama-provider models, so asserting the log's absence here would be vacuous.
- `tests/test_briefing.py` — a new case for `_assert_fixture_only_data()`'s extended notes-path check: monkeypatch `notes._default_notes_path()` to return a non-tmp path and assert the `RuntimeError` fires, alongside the existing DB/briefings-dir cases (see Architecture, "Small, explicit extension to `_assert_fixture_only_data()`").
- **Honesty disclaimer:** the stderr-substring test and the new proactive `opencode agent list` pre-check's test both necessarily mock/fake the subprocess call. Passing tests here prove the code's OWN logic is correct (it does what it's told the subprocess returned) — they do NOT prove opencode's real CLI output actually matches what's mocked. This mirrors the same honesty disclaimer the original gemma4 shadow-run design already carried for its own subprocess-adjacent test; acceptance criteria #1 and #3 below are where the real-opencode-binary gap gets closed, and those are manual/environment-dependent, not CI-automatable.
- Full suite + coverage gate re-run at the end, same as any other change.

## Docs to update

- `.env.example` — new `LOCAL_FITNESS_OPENCODE_AGENT` var + the exact
  `~/.config/opencode/opencode.jsonc` snippet required (tool-free agent).
- `docs/plans/2026-07-05-gemma4-shadow-run-design.md` — leave as historical
  record of the original numbers (referenced above), add a pointer note to
  this doc rather than rewriting it in place.
- `CLAUDE.md` — no existing section documents this feature today (confirmed);
  no update needed unless this graduates beyond shadow-run scope later.
- `CHANGELOG.md` + version bump, per this repo's release policy (functional
  change, not docs-only).

## Out of scope

- Promoting any non-Claude model to the live production 06:30 brief job —
  unchanged, explicitly future/separate, gated by a real quality comparison.
- Extending `scripts/ab_brief.py` (currently Claude-only) to also drive
  alt-model comparisons — not requested, not touched here.
- Any change to `_assert_fixture_only_data()`'s core allow-list *mechanism*
  (`.resolve().is_relative_to(tempfile.gettempdir())`) — it's already
  transport-agnostic and needs none. (The gate DOES gain one small, explicit
  extension in this design — a third checked path, `notes._default_notes_path()`
  — see "Small, explicit extension to `_assert_fixture_only_data()`" above;
  that's additive coverage of an existing hole, not a change to how the check
  works.)

## Acceptance criteria

1. The shadow-run scripts (`scripts/shadow_run.py`,
   `scripts/capture_baseline.py`) can generate a brief against
   `opencode/deepseek-v4-flash-free` end-to-end against fixture-only data,
   without touching real personal data. These are the only two scripts with
   an actual fixture-isolation harness (`_capture_v2`/`_capture_live`, which
   redirect `db.DEFAULT_DB_PATH`, `LOCAL_FITNESS_NOTES_PATH`, and
   `briefs.DEFAULT_BRIEFINGS_DIR` to tmp paths before calling) — `scripts/ab_brief.py`
   calls `briefing._generate(model=...)` directly against the real DB/notes/
   briefings with no isolation, so it is deliberately excluded here; see "Out
   of scope" above. There is no `fitness` CLI subcommand
   that accepts an alt-model — `fitness brief` is hardwired to Claude — so
   these two scripts are the only reachable entry points for this path;
   adding a CLI flag is out of scope for this design. **Manual/environment-
   dependent** — requires a real `opencode` binary, network access to its
   hosted gateway, and the user's own `~/.config/opencode/opencode.jsonc`
   agent config; not verifiable by the automated test suite alone.
   **Warning — `--out` must be overridden for alt-model runs:**
   `capture_baseline.py --run` defaults its `--out` argument to the committed
   `tests/evals/baseline.json`. The DB/notes/briefings-dir fixture isolation
   described above is fine — only the OUTPUT FILE is at risk — but running
   this script with default args against
   `opencode:opencode/deepseek-v4-flash-free` (or any alt model) would
   OVERWRITE the real committed Claude-V1 baseline with an alt-model capture.
   Anyone running `capture_baseline.py` against an alt-model MUST pass an
   explicit `--out` pointing somewhere other than the committed baseline
   path.
2. gemma4 via `"opencode:ollama/gemma4"` still gets its validated tightened
   schema + temperature 0.8 + plan-facts/status appendix — behavior
   unchanged from today, just re-keyed. Automated-suite-verifiable.
3. An unconfigured `LOCAL_FITNESS_OPENCODE_AGENT` (or a misconfigured opencode
   agent name) fails loudly with a clear, actionable `RuntimeError` — never
   silently falls back to a tool-enabled agent. "Fails loudly" here means
   specifically: the `RuntimeError` is raised (not swallowed as a success)
   and propagates uncaught out of `generate_streaming` — the alt-model
   dispatch branch returns before ever reaching the `try`/`except
   BaseException` block that wraps the Claude `query()` loop further down in
   `generate_streaming`, and that block is the only place in the module that
   calls `LOG.exception`. So today, **no log is emitted anywhere** specifically
   in this pre-check `RuntimeError`/failure path — not at ERROR, not to
   stderr, nowhere — this is unconditional and already true by construction
   for *this* failure path, but it is the *absence* of logging, not its
   presence. (This does not generalize to every alt-model failure mode: a
   post-generation parse/validation failure still logs via `_finalize_brief`'s
   `LOG.error` calls, and the threat-model section's opencode off-machine
   warning log fires for any opencode send that passes this pre-check.) That
   makes whether the actionable message text
   reaches the report a user actually reads entirely dependent on the fix
   described in "`scripts/shadow_run.py` must surface the failure message,
   not just a flake count" above (`_capture_v2` printing at least the
   exception's first line instead of only incrementing a flake count) —
   without that fix, the shadow-run harness's own printed report shows only
   `flakes=N`/`OVERALL: PARITY FAILED`, and the remediation pointer is lost
   entirely, since there is no log anywhere else for it to survive in. The
   unit-level logic (given a mocked `opencode agent list`/stderr output, the
   RuntimeError fires with the right message) is automated-suite-verifiable;
   confirming this against opencode's *real* fallback behavior, and
   confirming the message is actually visible in `shadow_run.py`'s printed
   report, is **manual/environment-dependent** (same caveat as the Testing
   strategy's honesty disclaimer above).
4. Full test suite green, coverage gate maintained. Automated-suite-verifiable.
