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
`if local_model_name == "gemma4"` branches in `briefing.py`. The user is
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
same string shape either way. `_local_model_name()` strips the prefix and
returns the full `provider/model` string; callers `.partition("/")` it to get
`provider` (routing decision) and `bare_model` (the literal value passed to
whichever client is chosen).

## Architecture

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
- Invokes `subprocess.run(["opencode", "run", message, "-m", model,
  "--format", "json", "--agent", agent], capture_output=True, text=True,
  timeout=timeout)`.
- Parses stdout as newline-delimited JSON events; concatenates every
  `type == "text"` event's `part.text` in order (there can be more than one
  text-part chunk before `step_finish`) and returns the joined string.
- **Safety check, not optional:** scans stderr for the literal substring
  `"not found. Falling back to default agent"` — confirmed by direct testing
  that opencode does NOT error on an unknown `--agent` name, it warns and
  silently falls back to the full-tool-access `build` agent (which then has
  live bash/edit access and costs ~30k tokens for a trivial reply in testing).
  If that substring is present, raise `RuntimeError` naming the missing agent
  and pointing at the `.env.example` setup snippet — never let the call
  silently proceed on the unsafe fallback.
- Raises `RuntimeError` (one normalized shape, mirroring `local_model.py`) on:
  `FileNotFoundError` (opencode binary missing), non-zero exit code,
  `subprocess.TimeoutExpired`, empty/no `text` events found, or any line that
  fails `json.loads`.

### Required manual setup (documented in `.env.example`)

A repo-local `opencode.json` is **not** auto-discovered by opencode (tested
directly) — so a lean, tool-free agent can only live in the user's own
personal `~/.config/opencode/opencode.jsonc`, same category as this project's
existing Garmin-credential / Claude-OAuth-token manual setup steps. New env
var `LOCAL_FITNESS_OPENCODE_AGENT` (default `"fitness-brief"`) names it.
`.env.example` documents the exact JSON snippet to add — an agent with every
`tools.*` key set `false`, mirroring the shape of the user's existing
`fitness` agent block.

### Per-model profile registry (replaces the `if == "gemma4"` branches)

```python
@dataclass(frozen=True)
class _ModelProfile:
    temperature: float = 0.4
    ollama_format_schema: Callable[[], dict] | None = None  # ollama provider only
    plan_prompt_facts: Callable[[dict], str] | None = None
    plan_status_appendix: Callable[[dict], str] | None = None
    reshape: Callable[[str], str] | None = None

_DEFAULT_PROFILE = _ModelProfile()
_MODEL_PROFILES: dict[str, _ModelProfile] = {
    "ollama/gemma4": _ModelProfile(
        temperature=0.8,
        ollama_format_schema=_gemma4_format_schema,
        plan_prompt_facts=_gemma4_plan_prompt_facts,
        plan_status_appendix=_gemma4_plan_status_appendix,
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

### `generate_streaming`'s restructured local-model branch

```python
local_model_name = _local_model_name(model)          # "ollama/gemma4" | "opencode/deepseek-v4-flash-free" | None
if local_model_name is not None:
    if not _brief_v2_enabled():
        raise ValueError(...)                         # unchanged
    brief_context = brief_planner.assemble_brief_context(...)
    provider, _, bare_model = local_model_name.partition("/")
    profile = _MODEL_PROFILES.get(local_model_name, _DEFAULT_PROFILE)

    local_system_prompt = prompts.brief_v2_system_prompt(user_name, coach_profile)
    local_prompt_text = prompts.brief_v2_user_prompt(...)
    if profile.plan_prompt_facts and brief_context.plan_today is not None:
        local_prompt_text += profile.plan_prompt_facts(brief_context.plan_today)

    _assert_fixture_only_data()                        # unchanged, still called before either transport

    if provider == "ollama":
        local_format = (profile.ollama_format_schema() if profile.ollama_format_schema
                         else Brief.model_json_schema())
        raw = (await asyncio.to_thread(
            local_model.generate_local_completion,
            local_system_prompt, local_prompt_text, model=bare_model,
            format=local_format, temperature=profile.temperature,
        )).strip()
    else:
        agent = os.environ.get("LOCAL_FITNESS_OPENCODE_AGENT", "fitness-brief")
        raw = (await asyncio.to_thread(
            opencode_model.generate_opencode_completion,
            local_system_prompt, local_prompt_text,
            model=local_model_name, agent=agent,
        )).strip()

    if profile.reshape:
        raw = profile.reshape(raw)
    if profile.plan_status_appendix and brief_context.plan_today is not None:
        raw = _append_gemma4_plan_status(raw, brief_context.plan_today)  # generalized to use profile.plan_status_appendix
```

`_assert_fixture_only_data()` is untouched — it doesn't reference a
transport or model at all, so it needs no changes and stays called in the
same position, before either transport fires.

### Dead-code cleanup (same commit)

`prompts.py`'s `brief_v2_system_prompt_gemma4`/`brief_v2_user_prompt_gemma4`
are confirmed unused in production (the round-2 finding already noted in
`briefing.py`'s comments: prompt-appendix tuning didn't help, only the
schema trick did) — delete them. Their three tests in `test_prompts.py`
(`test_gemma4_system_prompt_is_superset_of_base`,
`test_gemma4_system_prompt_names_observed_failures`,
`test_gemma4_user_prompt_is_superset_of_base`) are deleted in the same
change, not left dangling.

## API Surface

- `agent/opencode_model.py::generate_opencode_completion(system_prompt: str, user_prompt: str, *, model: str, agent: str, timeout: float = 300.0) -> str` — raises `RuntimeError` on any failure (binary missing, non-zero exit, timeout, malformed/empty output, or detected unsafe agent-fallback).
- `agent/briefing.py::_local_model_name(model: str) -> str | None` — now strips `"opencode:"` (was `"ollama:"`) and returns the full `provider/model` string unchanged (was: bare Ollama model name).
- `agent/briefing.py::_MODEL_PROFILES: dict[str, _ModelProfile]` and `_DEFAULT_PROFILE: _ModelProfile` — new module-level registry, additive-only for future models.
- `local_model.py::generate_local_completion(...)` — **unchanged** signature and behavior; still called for `provider == "ollama"`, now with a bare (unprefixed) model name.
- New env var `LOCAL_FITNESS_OPENCODE_AGENT` (default `"fitness-brief"`).

## Invariants

**Checkable (by inspection):**
- `_assert_fixture_only_data()` is called before either transport, unconditionally, for every non-Claude model — unchanged from today.
- No hardcoded model-name string equality remains in `briefing.py`'s dispatch or transport-selection logic — only the profile-registry lookup and the `provider == "ollama"` capability check.
- `agent/opencode_model.py` never imports or calls anything from `local_model.py` and vice versa — the two transports are independent, swappable per-provider.
- `LOCAL_ONLY`/production 06:30 job code path (`generate_and_save`'s default `model=DEFAULT_MODEL`) never reaches the local-model branch — `DEFAULT_MODEL` is Claude, unchanged.

**Testable:**
- A `"opencode:ollama/gemma4"` model string dispatches to `local_model.generate_local_completion` with `model="gemma4"` (bare, unprefixed) and the gemma4 profile's tightened schema/temperature.
- A `"opencode:opencode/deepseek-v4-flash-free"` (or any non-`ollama` provider) model string dispatches to `opencode_model.generate_opencode_completion`, never touches `local_model.py`.
- A model string with no matching `_MODEL_PROFILES` entry uses `_DEFAULT_PROFILE` (plain `Brief.model_json_schema()`, temperature 0.4, no plan-facts injection, no reshape) — proving new models get the safe generic path automatically.
- `generate_opencode_completion` raises `RuntimeError` for: non-zero exit, `FileNotFoundError`, `TimeoutExpired`, empty JSON-event stream, malformed JSON line, and the detected "falling back to default agent" stderr substring — each as its own test case, mirroring `test_local_model.py`'s existing per-failure-mode tests.
- `scripts/shadow_run.py`'s local-model detection (`args.model.startswith(...)`) imports and uses the shared prefix constant instead of re-declaring the literal string (fixes a real coupling gap the Impact Analyst found).
- `_assert_fixture_only_data()`'s existing three tests (passes under tmp path, raises for real DB path, raises for real briefings dir) are unchanged and still pass.

## Testing strategy

- `tests/test_opencode_model.py` (new) — mirrors `tests/test_local_model.py`'s structure, using `monkeypatch.setattr(opencode_model.subprocess, "run", ...)` (the existing convention from `tests/test_cli.py`, not a global `subprocess.run` patch) to feed fake `CompletedProcess` objects for each success/failure case.
- `tests/test_briefing.py` — every existing `"ollama:gemma4"`/`"ollama:llama3.1:8b"` literal is renamed to the new `"opencode:ollama/..."` shape; `test_local_model_name_prefix_parsing` gets new cases covering a non-Ollama provider string; a new test proves the profile-registry lookup (gemma4 gets its profile, an unlisted model gets `_DEFAULT_PROFILE`); a new test proves a non-Ollama dispatch never touches `local_model.py` (monkeypatch it to fail loudly if called).
- `tests/test_local_model.py` — unchanged except the calling convention in tests that exercise it via `briefing.py` (bare model name, not prefixed).
- `tests/test_prompts.py` — the 3 gemma4-prompt tests are deleted along with the functions.
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
- Any change to `_assert_fixture_only_data()`'s logic — it's already
  transport-agnostic and needs none.

## Acceptance criteria

1. `uv run fitness ...` (or the shadow-run scripts) can generate a brief
   against `opencode/deepseek-v4-flash-free` end-to-end against fixture-only
   data, without touching real personal data.
2. gemma4 via `"opencode:ollama/gemma4"` still gets its validated tightened
   schema + temperature 0.8 + plan-facts/status appendix — behavior
   unchanged from today, just re-keyed.
3. An unconfigured `LOCAL_FITNESS_OPENCODE_AGENT` (or a misconfigured opencode
   agent name) fails loudly with a clear, actionable `RuntimeError` — never
   silently falls back to a tool-enabled agent.
4. Full test suite green, coverage gate maintained.
