"""Tests for agent/briefing.py.

Two layers:

1. The PURE helpers — the streaming partial-JSON parser
   ``_iter_partial_takeaways`` and the env-var reader ``_brief_effort``.
   Mock-free; ``monkeypatch`` only sets/clears env vars.

2. The ``generate_streaming`` async generator's real control flow. It wraps
   the Claude Agent SDK ``query()``; rather than mock-glue, we monkeypatch
   ``briefing.query`` to a fake async generator that yields *real* SDK
   ``StreamEvent``/``AssistantMessage``/``UserMessage`` objects (constructed
   from the SDK's own dataclasses) carrying fabricated brief text, and assert
   real outcomes: a brief is written to disk (or NOT, when ``save=False``),
   takeaways stream out incrementally with monotonic indices, parse/validation
   failures surface ``error`` events without writing a file, the nested-takeaway
   salvage path is applied, and a mid-stream exception propagates. Every
   assertion can fail if the loop's logic were broken (save skipped, validation
   bypassed, indices wrong, error swallowed). The DB + briefings dir are
   redirected at a tmp path so the live brief is never touched.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from local_fitness import db
from local_fitness import notes
from local_fitness.agent import briefing
from local_fitness.agent import briefs
from local_fitness.agent import coach
from local_fitness.agent import prompts
from local_fitness.agent.schemas import Brief, BriefContext


# --- _iter_partial_takeaways: the streaming partial-JSON parser ------------
#
# Contract (read from briefing.py:96-134): a generator. It locates the
# ``"takeaways"`` key, then the opening ``[``, then raw_decodes one object at
# a time. It yields each *dict* whose 1-based parse position exceeds
# ``skip_count``. It stops (returns) on incomplete/malformed JSON, on the
# closing ``]``, or at end of text. A leading ```json fence is stripped.

def _collect(text: str, skip_count: int = 0) -> list[dict]:
    return list(briefing._iter_partial_takeaways(text, skip_count))


def test_empty_input_yields_nothing():
    assert _collect("") == []


def test_no_takeaways_key_yields_nothing():
    assert _collect('{"date": "2026-06-25", "summary": "hi"}') == []


def test_takeaways_key_but_no_open_bracket_yields_nothing():
    # The key is present but the array hasn't started streaming yet.
    assert _collect('{"takeaways"') == []
    assert _collect('{"takeaways":') == []


def test_single_complete_object_is_yielded():
    text = '{"takeaways": [{"headline": "Easy 5k", "tone": "positive"}]}'
    out = _collect(text)
    assert len(out) == 1
    assert out[0] == {"headline": "Easy 5k", "tone": "positive"}


def test_partial_trailing_object_is_not_yielded_yet():
    # First object is complete; second is still streaming in (no closing brace).
    text = (
        '{"takeaways": [{"headline": "first", "tone": "neutral"},'
        ' {"headline": "secon'
    )
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["first"]


def test_incomplete_first_object_yields_nothing():
    # The very first object is unterminated → raw_decode raises → stop.
    text = '{"takeaways": [{"headline": "Easy 5k", "tone": "posi'
    assert _collect(text) == []


def test_multiple_complete_objects_all_yielded():
    text = (
        '{"takeaways": ['
        '{"headline": "a", "tone": "neutral"},'
        '{"headline": "b", "tone": "positive"},'
        '{"headline": "c", "tone": "warning"}'
        ']}'
    )
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["a", "b", "c"]


def test_skip_count_skips_already_yielded_items():
    text = (
        '{"takeaways": ['
        '{"headline": "a", "tone": "neutral"},'
        '{"headline": "b", "tone": "positive"},'
        '{"headline": "c", "tone": "warning"}'
        ']}'
    )
    # skip_count=2 → only the 3rd (found > skip_count) is yielded.
    out = _collect(text, skip_count=2)
    assert [tk["headline"] for tk in out] == ["c"]


def test_skip_count_at_or_above_total_yields_nothing():
    text = '{"takeaways": [{"headline": "a", "tone": "neutral"}]}'
    assert _collect(text, skip_count=1) == []
    assert _collect(text, skip_count=5) == []


def test_boundary_object_completes_as_text_grows():
    # Simulate a growing LLM response: the second takeaway is only yielded
    # once its closing brace arrives.
    partial = '{"takeaways": [{"headline": "a", "tone": "neutral"}, {"headline": "b"'
    grown = partial + ', "tone": "positive"}]}'

    before = [tk["headline"] for tk in _collect(partial)]
    after = [tk["headline"] for tk in _collect(grown)]

    assert before == ["a"]
    assert after == ["a", "b"]
    # The streaming caller would pass skip_count=1 after yielding "a", so on
    # the grown text it emits only the newly-complete "b".
    assert [tk["headline"] for tk in _collect(grown, skip_count=1)] == ["b"]


def test_closing_bracket_stops_iteration():
    # Empty array: hits ``]`` immediately → no objects.
    assert _collect('{"takeaways": []}') == []


def test_leading_json_fence_is_stripped():
    text = '```json\n{"takeaways": [{"headline": "fenced", "tone": "neutral"}]}'
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["fenced"]


def test_bare_triple_backtick_fence_is_stripped():
    text = '```\n{"takeaways": [{"headline": "bare", "tone": "neutral"}]}'
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["bare"]


def test_inline_control_chars_stripped_before_parse():
    # A raw control char inside a string value would make strict JSON choke;
    # _strip_inline_control_chars (applied inside the helper) removes it so the
    # object still parses.
    text = '{"takeaways": [{"headline": "run\x07logged", "tone": "positive"}]}'
    out = _collect(text)
    assert len(out) == 1
    assert out[0]["headline"] == "runlogged"


def test_malformed_fragment_after_good_object_stops_cleanly():
    # First object parses; the next token is garbage (not an object) → the
    # parser stops without raising, having yielded the good one.
    text = '{"takeaways": [{"headline": "good", "tone": "neutral"}, not-json-here'
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["good"]


def test_whitespace_and_commas_between_objects_are_skipped():
    text = (
        '{"takeaways": [\n'
        '  {"headline": "a", "tone": "neutral"} ,\n\n'
        '  {"headline": "b", "tone": "positive"}\n'
        ']}'
    )
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["a", "b"]


def test_non_dict_array_items_are_not_yielded():
    # raw_decode succeeds on a non-dict (a string), but the ``isinstance(obj,
    # dict)`` guard means it is not yielded; the dict after it still is.
    text = '{"takeaways": ["stray", {"headline": "real", "tone": "neutral"}]}'
    out = _collect(text)
    assert [tk["headline"] for tk in out] == ["real"]


# --- _brief_effort: env-var matrix -----------------------------------------

_ENV = "LOCAL_FITNESS_BRIEF_EFFORT"


def test_brief_effort_unset_returns_default(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert briefing._brief_effort() == briefing._DEFAULT_BRIEF_EFFORT
    assert briefing._brief_effort() == "low"


def test_brief_effort_valid_values_passthrough(monkeypatch):
    for val in ("low", "medium", "high", "max"):
        monkeypatch.setenv(_ENV, val)
        assert briefing._brief_effort() == val


def test_brief_effort_normalizes_case_and_whitespace(monkeypatch):
    monkeypatch.setenv(_ENV, "  MAX  ")
    assert briefing._brief_effort() == "max"
    monkeypatch.setenv(_ENV, "High")
    assert briefing._brief_effort() == "high"


def test_brief_effort_invalid_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(_ENV, "turbo")
    assert briefing._brief_effort() == briefing._DEFAULT_BRIEF_EFFORT


def test_brief_effort_empty_string_falls_back_to_default(monkeypatch):
    # Set-but-empty is distinct from unset: it is not None, strips to "", which
    # is not a valid effort → default.
    monkeypatch.setenv(_ENV, "   ")
    assert briefing._brief_effort() == briefing._DEFAULT_BRIEF_EFFORT


# === generate_streaming: real control-flow over a fake SDK query ===========
#
# We drive the genuine loop by replacing ``briefing.query`` with an async
# generator that yields real SDK message objects. The brief JSON is fabricated
# and fed through the same StreamEvent text-delta path the live model uses, so
# the partial parser, the post-loop validation/save gate, and the error/done
# branches all execute for real. The briefings dir + DB are redirected to tmp.


@pytest.fixture
def stream_env(tmp_path, monkeypatch):
    """Redirect brief I/O + the DB at a tmp dir. Returns the briefings dir.

    ``save_brief`` and ``_recent_briefs_summary`` both read
    ``briefs.DEFAULT_BRIEFINGS_DIR`` (the module global), so patching it there
    routes every write/read through tmp.
    """
    out = tmp_path / "briefings"
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", out)
    dbp = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", dbp)
    db.init_schema(dbp)
    # Isolate notes so neither prompt path reads the real user_notes.md.
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    return out


_TAKEAWAY = {
    "headline": "Easy 5k on tap",
    "summary": "RHR steady and TSB positive — green light to run.",
    "tone": "positive",
    "details": "Full markdown deep-dive goes here.",
}


def _takeaway(**over) -> dict:
    tk = dict(_TAKEAWAY)
    tk.update(over)
    return tk


def _text_event(text: str) -> StreamEvent:
    """A partial-message StreamEvent carrying a text delta — the only event
    shape ``generate_streaming`` extracts brief text from."""
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _brief_json(takeaways: list[dict]) -> str:
    return json.dumps({"takeaways": takeaways})


def _split(s: str, n: int) -> list[str]:
    """Slice ``s`` into ``n`` chunks (mid-object boundaries on purpose, to
    exercise the parser's accumulate-across-deltas behavior)."""
    size = max(1, len(s) // n)
    return [s[i : i + size] for i in range(0, len(s), size)]


def _install_query(monkeypatch, messages, raise_at_end: BaseException | None = None):
    async def fake_query(prompt, options):  # noqa: ARG001 — signature-compat
        for m in messages:
            yield m
        if raise_at_end is not None:
            raise raise_at_end

    monkeypatch.setattr(briefing, "query", fake_query)


def _install_query_capture(monkeypatch, messages) -> dict:
    """Like _install_query but records the prompt + options the path passed —
    so a test can assert V1-tools vs V2-toolless routing."""
    captured: dict = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        for m in messages:
            yield m

    monkeypatch.setattr(briefing, "query", fake_query)
    return captured


def _drain(save: bool = True) -> list[dict]:
    async def go():
        return [evt async for evt in briefing.generate_streaming(save=save)]

    return asyncio.run(go())


def test_streaming_saves_brief_to_disk_and_emits_done(stream_env, monkeypatch):
    # Whole brief lands in one text delta. Expect: takeaway event(s), a done
    # event whose brief matches what was written to disk.
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    events = _drain(save=True)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert not [e for e in events if e["type"] == "error"]

    today = date_today()
    written = stream_env / f"{today}.json"
    assert written.exists(), "save=True must persist the brief before done"

    on_disk = json.loads(written.read_text())
    assert on_disk["takeaways"][0]["headline"] == _TAKEAWAY["headline"]
    # The streamed done brief is the SAME validated object written to disk.
    assert done[0]["brief"]["takeaways"][0]["headline"] == _TAKEAWAY["headline"]
    assert done[0]["brief"]["date"] == today


def test_streaming_save_false_does_not_write(stream_env, monkeypatch):
    # save=False (eval path): validates + emits done but must NOT clobber disk.
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    events = _drain(save=False)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["brief"]["takeaways"][0]["headline"] == _TAKEAWAY["headline"]
    # The decisive assertion: no file written on the save=False branch.
    assert list(stream_env.glob("*.json")) == []
    assert not stream_env.exists() or list(stream_env.glob("*.json")) == []


def test_streaming_emits_takeaways_incrementally_with_monotonic_index(
    stream_env, monkeypatch
):
    # Three distinct takeaways streamed across many partial deltas. Each must be
    # emitted exactly once, in order, with a monotonically increasing index —
    # and the final done brief must contain all three.
    takeaways = [
        _takeaway(headline="First insight"),
        _takeaway(headline="Second insight"),
        _takeaway(headline="Third insight"),
    ]
    deltas = _split(_brief_json(takeaways), 8)
    _install_query(monkeypatch, [_text_event(d) for d in deltas])
    events = _drain(save=True)

    tk_events = [e for e in events if e["type"] == "takeaway"]
    assert [e["index"] for e in tk_events] == [0, 1, 2]
    assert [e["takeaway"]["headline"] for e in tk_events] == [
        "First insight",
        "Second insight",
        "Third insight",
    ]
    done = [e for e in events if e["type"] == "done"][0]
    assert [t["headline"] for t in done["brief"]["takeaways"]] == [
        "First insight",
        "Second insight",
        "Third insight",
    ]


def test_streaming_unparseable_output_emits_error_and_saves_nothing(
    stream_env, monkeypatch
):
    # Model emits prose with no JSON object → _extract_json raises → error event,
    # no done, nothing written.
    _install_query(monkeypatch, [_text_event("Sorry, I can't produce that today.")])
    events = _drain(save=True)

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "parse" in errors[0]["message"].lower()
    assert not [e for e in events if e["type"] == "done"]
    assert list(stream_env.glob("*.json")) == [] if stream_env.exists() else True


def test_streaming_empty_stream_emits_error_without_crashing(stream_env, monkeypatch):
    # query yields nothing at all (e.g. the model returned before any text).
    # The loop must exit cleanly and the empty-buffer parse must surface an
    # error event rather than raising.
    _install_query(monkeypatch, [])
    events = _drain(save=True)

    assert [e["type"] for e in events] == ["error"]
    assert not (stream_env.exists() and list(stream_env.glob("*.json")))


def test_streaming_invalid_brief_emits_validation_error_no_file(stream_env, monkeypatch):
    # JSON parses but a takeaway has a tone outside the enum → save_brief raises
    # ValidationError → error event, no done, no file. Proves validation is NOT
    # bypassed on the save path.
    bad = _brief_json([_takeaway(tone="ecstatic")])
    _install_query(monkeypatch, [_text_event(bad)])
    events = _drain(save=True)

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "validation" in errors[0]["message"].lower()
    assert not [e for e in events if e["type"] == "done"]
    assert not (stream_env.exists() and list(stream_env.glob("*.json")))


def test_streaming_save_false_invalid_brief_emits_validation_error(
    stream_env, monkeypatch
):
    # The save=False branch validates locally via Brief.model_validate — a too-
    # large takeaways list (>5) must surface an error, not a done.
    bad = _brief_json([_takeaway(headline=f"tk{i}") for i in range(6)])
    _install_query(monkeypatch, [_text_event(bad)])
    events = _drain(save=False)

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "validation" in errors[0]["message"].lower()
    assert not [e for e in events if e["type"] == "done"]


def test_streaming_salvages_nested_takeaways_and_saves(stream_env, monkeypatch):
    # Model wraps takeaways under a sibling key (a user note can induce this).
    # _extract_json's salvage recovers them; the brief still validates + saves.
    nested = json.dumps({"wrapper": {"takeaways": [_takeaway(headline="Salvaged")]}})
    _install_query(monkeypatch, [_text_event(nested)])
    events = _drain(save=True)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["brief"]["takeaways"][0]["headline"] == "Salvaged"
    written = stream_env / f"{date_today()}.json"
    assert written.exists()
    assert json.loads(written.read_text())["takeaways"][0]["headline"] == "Salvaged"


def test_streaming_processes_tool_messages_then_saves(stream_env, monkeypatch, caplog):
    # Interleave a tool-use AssistantMessage (with a usage payload) and its
    # tool-result UserMessage BEFORE the brief text. The loop's tool-instrument
    # branches must run (proven via the brief_timing tool_use log line) and the
    # brief must still parse + save afterwards.
    messages = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="get_today_status", input={})],
            model="m",
            usage={"output_tokens": 123, "input_tokens": 456},
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="x" * 40)]),
        _text_event(_brief_json([_takeaway()])),
    ]
    _install_query(monkeypatch, messages)

    with caplog.at_level(logging.INFO, logger="local_fitness.agent.briefing"):
        events = _drain(save=True)

    # The tool branch actually executed (not just that the brief saved).
    assert "phase=tool_use name=get_today_status" in caplog.text
    assert "phase=tool_result name=get_today_status" in caplog.text
    # And the brief still went through to a saved done.
    assert [e for e in events if e["type"] == "done"]
    assert (stream_env / f"{date_today()}.json").exists()


def test_streaming_propagates_mid_stream_exception(stream_env, monkeypatch):
    # A failure inside query() (BaseException branch) must propagate out of the
    # generator, not be swallowed into a silent no-save. Some text streamed
    # first, so the exception handler's "errored mid-flight" path runs.
    _install_query(
        monkeypatch,
        [_text_event('{"takeaways": [')],
        raise_at_end=RuntimeError("upstream SDK blew up"),
    )
    with pytest.raises(RuntimeError, match="upstream SDK blew up"):
        _drain(save=True)
    # Nothing persisted on a mid-stream failure.
    assert not (stream_env.exists() and list(stream_env.glob("*.json")))


def date_today() -> str:
    from datetime import date

    return date.today().isoformat()


# --- Phase 0: _generate is a save=False eval/read helper (never clobbers live) --

def test_generate_save_false_does_not_write_live_brief(stream_env, monkeypatch):
    # _generate is the A/B-harness/read helper; it must NEVER overwrite
    # briefings/<date>.json (that's what made `ab_brief --run` dangerous).
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    brief = asyncio.run(briefing._generate(model="m"))            # save defaults False
    assert brief.takeaways[0].headline == _TAKEAWAY["headline"]   # returns the Brief
    assert list(stream_env.glob("*.json")) == []                 # nothing written


def test_generate_save_true_writes(stream_env, monkeypatch):
    # the explicit save=True path still persists (the production save path uses it).
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    asyncio.run(briefing._generate(model="m", save=True))
    assert list(stream_env.glob("*.json"))


# --- Phase 3a: LOCAL_FITNESS_BRIEF_V2 flag + toolless routing ---------------

def test_brief_v2_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_V2", raising=False)
    assert briefing._brief_v2_enabled() is True


@pytest.mark.parametrize("val,on", [("1", True), ("true", True), ("on", True),
                                    ("YES", True), ("", True), ("maybe", True),
                                    ("0", False), ("false", False), ("no", False),
                                    ("off", False), ("OFF", False)])
def test_brief_v2_flag_parsing(monkeypatch, val, on):
    # Default-ON: only an explicit 0/false/no/off rolls back to V1.
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", val)
    assert briefing._brief_v2_enabled() is on


def test_v1_fallback_routes_tools(stream_env, monkeypatch):
    """LOCAL_FITNESS_BRIEF_V2=0 → the V1 monolith fallback: MCP server attached,
    max_turns=20, Step-1 tool orchestration in the prompt."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "0")
    cap = _install_query_capture(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    _drain(save=False)
    assert cap["options"].mcp_servers and cap["options"].max_turns == 20
    assert "get_training_plan_status" in cap["prompt"]


def test_v2_flag_routes_toolless_generator_and_saves(stream_env, monkeypatch):
    """Flag ON → planner pre-pass + a single TOOLLESS generator: no MCP server,
    max_turns=1, the prompt carries the pre-fetched BriefContext (no tool list).
    The shared stream/parse/save core still validates + persists the brief."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    cap = _install_query_capture(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    events = _drain(save=True)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and not [e for e in events if e["type"] == "error"]
    # Toolless options — the invariant that makes grounding sound.
    assert not cap["options"].mcp_servers
    assert cap["options"].max_turns == 1
    # Prompt is BriefContext-driven, not V1 tool-orchestration.
    assert "cite ONLY these numbers" in cap["prompt"]
    assert "get_training_plan_status" not in cap["prompt"]
    # Saved through the same gate as V1.
    assert (stream_env / f"{date_today()}.json").exists()


def test_v2_path_streams_takeaways_and_validates(stream_env, monkeypatch):
    """The V2 path reuses the streaming partial-parser + validation gate, so a
    malformed stream still surfaces an error (no silent empty brief)."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    _install_query(monkeypatch, [_text_event("not json at all")])
    events = _drain(save=True)
    assert [e for e in events if e["type"] == "error"]
    assert list(stream_env.glob("*.json")) == []


def test_v2_logs_grounding_signal_without_altering_brief(stream_env, monkeypatch, caplog):
    """The V2 path runs the advisory grounding check post-validation: it logs an
    invention-rate signal and leaves the brief byte-identical (never gates)."""
    import logging
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    with caplog.at_level(logging.INFO, logger="local_fitness.agent.grounding"):
        events = _drain(save=True)
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert any("brief_grounding" in r.message and "invention_rate" in r.message
               for r in caplog.records)
    # The advisory signal must not have changed the saved brief.
    assert done[0]["brief"]["takeaways"][0]["headline"] == _TAKEAWAY["headline"]


def test_v1_path_does_not_run_grounding(stream_env, monkeypatch, caplog):
    """V1 (fallback) has no BriefContext → no grounding log."""
    import logging
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "0")
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    with caplog.at_level(logging.INFO, logger="local_fitness.agent.grounding"):
        _drain(save=True)
    assert not any("brief_grounding" in r.message for r in caplog.records)


# --- alt-model (gemma4/Ollama + opencode) shadow-run dispatch ---------------
# See docs/plans/2026-07-08-model-agnostic-shadow-run-design.md (generalizes
# docs/plans/2026-07-05-gemma4-shadow-run-design.md's original gemma4/Ollama-
# only path). These test the "opencode:"-prefixed model dispatch in isolation
# from any real network call: briefing.local_model.generate_local_completion
# is monkeypatched to a plain (sync) fake, matching how asyncio.to_thread
# calls it in production.

def _drain_model(model: str, save: bool = False) -> list[dict]:
    async def go():
        return [evt async for evt in briefing.generate_streaming(model=model, save=save)]

    return asyncio.run(go())


@pytest.mark.parametrize("model,expected", [
    ("opencode:ollama/gemma4", "ollama/gemma4"),
    ("opencode:ollama/llama3.1:8b", "ollama/llama3.1:8b"),
    ("opencode:opencode/deepseek-v4-flash-free", "opencode/deepseek-v4-flash-free"),
    ("claude-sonnet-4-6", None),
    ("gemma4", None),  # no prefix -> not treated as alt-model, even if the name matches
])
def test_alt_model_name_prefix_parsing(model, expected):
    assert briefing._alt_model_name(model) == expected


def test_assert_fixture_only_data_passes_under_tmp_path(stream_env):
    # stream_env's fixture already points db.DEFAULT_DB_PATH and
    # briefs.DEFAULT_BRIEFINGS_DIR at pytest's tmp_path, which is guaranteed
    # to live under the system temp root.
    briefing._assert_fixture_only_data()  # must not raise


def test_assert_fixture_only_data_raises_for_real_db_path(monkeypatch, stream_env):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db._PROJECT_ROOT / "data" / "fitness.db")
    with pytest.raises(RuntimeError, match="db.DEFAULT_DB_PATH"):
        briefing._assert_fixture_only_data()


def test_assert_fixture_only_data_raises_for_real_briefings_dir(monkeypatch, stream_env):
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", db._PROJECT_ROOT / "briefings")
    with pytest.raises(RuntimeError, match="briefs.DEFAULT_BRIEFINGS_DIR"):
        briefing._assert_fixture_only_data()


def test_assert_fixture_only_data_raises_for_real_notes_path(monkeypatch, stream_env):
    monkeypatch.setattr(notes, "_default_notes_path",
                        lambda: db._PROJECT_ROOT / "data" / "user_notes.md")
    with pytest.raises(RuntimeError, match=r"notes\._default_notes_path\(\)"):
        briefing._assert_fixture_only_data()


def test_local_model_refused_under_v1(stream_env, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "0")
    with pytest.raises(ValueError, match="toolless path"):
        _drain_model("opencode:ollama/gemma4", save=False)


def _gemma4_slot_json(workout: dict, steps: dict, others: list[dict]) -> str:
    """A gemma4 response in the explicit-slot shape _gemma4_format_schema
    asks for (see briefing._reshape_gemma4_slots)."""
    return json.dumps({
        "workout_takeaway": workout, "steps_takeaway": steps,
        "other_takeaways": others,
    })


def test_local_model_routes_to_local_client_and_saves(stream_env, monkeypatch):
    """V2 + an 'opencode:ollama/...' model calls local_model.generate_local_completion
    (not claude_agent_sdk.query) and flows through the same parse/validate/
    save/grounding tail as the Claude path. gemma4's response uses the
    explicit-slot shape (see _gemma4_format_schema) which gets flattened back
    into the real 'takeaways' list before that shared tail ever sees it."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    calls: list[dict] = []

    def fake_local_completion(system_prompt, user_prompt, *, model, format=None,  # noqa: A002
                              temperature=None, **kw):
        calls.append({"system": system_prompt, "user": user_prompt, "model": model,
                      "format": format, "temperature": temperature})
        return _gemma4_slot_json(
            _takeaway(headline="Today's workout"),
            _takeaway(headline="Steps check-in"),
            [_takeaway(headline="Conditioning note")],
        )

    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        fake_local_completion)
    events = _drain_model("opencode:ollama/gemma4", save=True)

    assert len(calls) == 1 and calls[0]["model"] == "gemma4"
    # Same prompt-construction call the Claude V2 path uses — parity by
    # construction, not by discipline.
    assert "cite ONLY these numbers" in calls[0]["user"]
    # Structured-output constraint, tightened for gemma4 specifically (see
    # _gemma4_format_schema) — forces schema-conformant, non-defaulted JSON
    # via grammar-constrained decoding rather than relying on instructions.
    assert calls[0]["format"] == briefing._gemma4_format_schema()
    assert calls[0]["temperature"] == 0.8
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and not [e for e in events if e["type"] == "error"]
    on_disk = json.loads((stream_env / f"{date_today()}.json").read_text())
    # Reshaped: workout first, steps second, then the rest — see
    # _reshape_gemma4_slots.
    assert [tk["headline"] for tk in on_disk["takeaways"]] == [
        "Today's workout", "Steps check-in", "Conditioning note"]


def test_local_model_parse_failure_surfaces_error_and_does_not_save(stream_env, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        lambda *a, **kw: "not json at all")
    events = _drain_model("opencode:ollama/gemma4", save=True)
    assert [e for e in events if e["type"] == "error"]
    assert list(stream_env.glob("*.json")) == []


def test_local_model_all_models_get_the_same_base_prompt(stream_env, monkeypatch):
    """Every local model gets the shared (Claude-identical) V2 prompt —
    round 2 (2026-07-05) found a gemma4-specific prompt appendix did NOT
    improve compliance, so gemma4-specific tuning now happens at the format-
    schema layer (_gemma4_format_schema), not via divergent prompt wording."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    calls: list[dict] = []
    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        lambda sp, up, **kw: calls.append({"system": sp, "user": up})
                        or _brief_json([_takeaway()]))
    expected_system_prompt = prompts.brief_v2_system_prompt(
        db.get_setting("user_name", prompts.DEFAULT_USER_NAME),
        coach.resolve_coach_profile())
    for model in ("opencode:ollama/gemma4", "opencode:ollama/llama3.1:8b"):
        calls.clear()
        _drain_model(model, save=False)
        assert calls[0]["system"] == expected_system_prompt


def test_local_model_gemma4_gets_tightened_schema_and_higher_temperature(stream_env, monkeypatch):
    """gemma4 specifically gets the tightened format schema + temperature=0.8
    (fixed the tone/metric defaulting behavior — see _gemma4_format_schema).
    A different local model gets the plain schema + the conservative default
    temperature, since that tuning was never validated for it."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    calls: list[dict] = []
    monkeypatch.setattr(
        briefing.local_model, "generate_local_completion",
        lambda sp, up, *, format=None, temperature=None, **kw: (  # noqa: A002
            calls.append({"format": format, "temperature": temperature})
            or _brief_json([_takeaway()])))

    calls.clear()
    _drain_model("opencode:ollama/gemma4", save=False)
    assert calls[0]["format"] == briefing._gemma4_format_schema()
    assert calls[0]["temperature"] == 0.8

    calls.clear()
    _drain_model("opencode:ollama/llama3.1:8b", save=False)
    assert calls[0]["format"] == Brief.model_json_schema()
    assert calls[0]["temperature"] == 0.4


def test_gemma4_format_schema_tightens_tone_and_metric():
    schema = briefing._gemma4_format_schema()
    takeaway = schema["$defs"]["Takeaway"]
    assert "default" not in takeaway["properties"]["tone"]
    assert takeaway["properties"]["metric"] == {
        "$ref": "#/$defs/TakeawayMetric",
        "description": "What metric to chart inline",
    }
    assert set(takeaway["required"]) == {
        "headline", "summary", "tone", "metric", "details"}


def test_gemma4_format_schema_expresses_category_mandate_as_required_slots():
    """The prompt mandates exactly one workout + one steps takeaway in every
    brief. A single `contains` constraint reliably forced one mandated
    category in testing, but `allOf`-combined `contains` (forcing both) was
    NOT reliably honored by Ollama's grammar-constrained decoder (verified
    2026-07-06 — one fixture produced zero of either mandated category across
    3 runs despite the constraint, and still validated). Required object keys
    have been reliable throughout, so the mandate is two required slots
    instead — verified 9/9 across 3 fixtures before being wired in here."""
    schema = briefing._gemma4_format_schema()
    assert "takeaways" not in schema["properties"]
    assert schema["properties"]["workout_takeaway"] == {"$ref": "#/$defs/Takeaway"}
    assert schema["properties"]["steps_takeaway"] == {"$ref": "#/$defs/Takeaway"}
    assert schema["properties"]["other_takeaways"] == {
        "type": "array", "items": {"$ref": "#/$defs/Takeaway"},
        "minItems": 1, "maxItems": 3,
    }
    assert set(schema["required"]) >= {
        "workout_takeaway", "steps_takeaway", "other_takeaways"}
    assert "takeaways" not in schema["required"]


def test_gemma4_format_schema_does_not_mutate_the_real_schema():
    """Calling _gemma4_format_schema() must not leak mutations back into
    Brief.model_json_schema()'s cached/shared $defs — each call builds a
    fresh copy."""
    before = Brief.model_json_schema()
    briefing._gemma4_format_schema()
    after = Brief.model_json_schema()
    assert before == after
    assert "default" in after["$defs"]["Takeaway"]["properties"]["tone"]
    assert "takeaways" in after["properties"]


def test_reshape_gemma4_slots_flattens_into_takeaways_list():
    raw = _gemma4_slot_json(
        _takeaway(headline="Workout"), _takeaway(headline="Steps"),
        [_takeaway(headline="Conditioning"), _takeaway(headline="HR recovery")],
    )
    reshaped = json.loads(briefing._reshape_gemma4_slots(raw))
    assert "workout_takeaway" not in reshaped
    assert "steps_takeaway" not in reshaped
    assert "other_takeaways" not in reshaped
    assert [tk["headline"] for tk in reshaped["takeaways"]] == [
        "Workout", "Steps", "Conditioning", "HR recovery"]


def test_reshape_gemma4_slots_handles_missing_other_takeaways():
    raw = json.dumps({
        "workout_takeaway": _takeaway(headline="Workout"),
        "steps_takeaway": _takeaway(headline="Steps"),
    })
    reshaped = json.loads(briefing._reshape_gemma4_slots(raw))
    assert [tk["headline"] for tk in reshaped["takeaways"]] == ["Workout", "Steps"]


def test_reshape_gemma4_slots_returns_raw_unchanged_when_slots_absent():
    """A response already in the plain 'takeaways' shape (e.g. a non-gemma4
    local model, or a malformed response) passes through untouched so the
    normal parse/validation path reports the real outcome."""
    raw = _brief_json([_takeaway()])
    assert briefing._reshape_gemma4_slots(raw) == raw
    assert briefing._reshape_gemma4_slots("not json at all") == "not json at all"


def test_local_model_client_failure_propagates(stream_env, monkeypatch):
    """A generate_local_completion failure (e.g. Ollama unreachable) is not
    swallowed here — it propagates so callers (shadow_run.py's per-run
    try/except) record it as a flake rather than a silent empty result."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))

    def boom(*a, **kw):
        raise RuntimeError("ollama call failed (connection): refused")

    monkeypatch.setattr(briefing.local_model, "generate_local_completion", boom)
    with pytest.raises(RuntimeError, match="ollama call failed"):
        _drain_model("opencode:ollama/gemma4", save=False)


def test_malformed_alt_model_string_raises_value_error(stream_env, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    with pytest.raises(ValueError, match="malformed alt-model string"):
        _drain_model("opencode:gemma4", save=False)  # no "/" -> no provider/model split


def test_unlisted_model_uses_default_profile():
    """A model with no _MODEL_PROFILES entry gets the safe generic path:
    plain schema, conservative temperature, no plan-facts, no reshape."""
    profile = briefing._MODEL_PROFILES.get(
        "opencode/deepseek-v4-flash-free", briefing._DEFAULT_PROFILE)
    assert profile is briefing._DEFAULT_PROFILE
    assert profile.temperature == 0.4
    assert profile.ollama_format_schema is None
    assert profile.plan_prompt_facts is None
    assert profile.plan_status_appendix is None
    assert profile.reshape is None


def test_gemma4_key_resolves_to_its_tuned_profile():
    profile = briefing._MODEL_PROFILES.get("ollama/gemma4", briefing._DEFAULT_PROFILE)
    assert profile.temperature == 0.8
    assert profile.ollama_format_schema is briefing._gemma4_format_schema
    assert profile.plan_prompt_facts is briefing._gemma4_plan_prompt_facts
    assert profile.plan_status_appendix is briefing._append_gemma4_plan_status
    assert profile.reshape is briefing._reshape_gemma4_slots


def test_non_ollama_dispatch_never_touches_local_model(stream_env, monkeypatch):
    """A non-'ollama' provider routes through opencode_model exclusively —
    local_model.generate_local_completion must never be called."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))

    def boom(*a, **kw):
        raise AssertionError("local_model.generate_local_completion must not be called")

    monkeypatch.setattr(briefing.local_model, "generate_local_completion", boom)
    monkeypatch.setattr(
        briefing.opencode_model, "generate_opencode_completion",
        lambda *a, **kw: _brief_json([_takeaway()]))
    events = _drain_model("opencode:opencode/deepseek-v4-flash-free", save=False)
    assert [e for e in events if e["type"] == "done"]


def test_non_ollama_dispatch_passes_full_provider_model_string(stream_env, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    calls: list[dict] = []

    def fake_opencode_completion(system_prompt, user_prompt, *, model, agent, **kw):
        calls.append({"model": model, "agent": agent})
        return _brief_json([_takeaway()])

    monkeypatch.setattr(briefing.opencode_model, "generate_opencode_completion",
                        fake_opencode_completion)
    _drain_model("opencode:opencode/deepseek-v4-flash-free", save=False)
    assert calls[0]["model"] == "opencode/deepseek-v4-flash-free"
    assert calls[0]["agent"] == "fitness-brief"


def test_opencode_agent_env_var_flows_into_dispatch(stream_env, monkeypatch):
    """Setting LOCAL_FITNESS_OPENCODE_AGENT to a distinct value must reach
    generate_opencode_completion's agent= argument — not the hardcoded
    default — proving the env var actually flows through, not just gets
    read and silently ignored."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setenv("LOCAL_FITNESS_OPENCODE_AGENT", "my-custom-agent")
    calls: list[dict] = []

    def fake_opencode_completion(system_prompt, user_prompt, *, model, agent, **kw):
        calls.append({"model": model, "agent": agent})
        return _brief_json([_takeaway()])

    monkeypatch.setattr(briefing.opencode_model, "generate_opencode_completion",
                        fake_opencode_completion)
    _drain_model("opencode:opencode/deepseek-v4-flash-free", save=False)
    assert calls[0]["agent"] == "my-custom-agent"


def test_profile_driven_plan_status_appendix_is_dispatched(stream_env, monkeypatch):
    """The profile-registry's plan_status_appendix callable is what's
    invoked — not a hardcoded call to _append_gemma4_plan_status — proven by
    swapping the registry entry for a stub and confirming the stub fires."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setattr(briefing.brief_planner, "assemble_brief_context",
                        lambda **kw: _fake_plan_context(_PLAN_TODAY))
    stub_calls: list[tuple] = []

    def stub_appendix(raw, plan_today):
        stub_calls.append((raw, plan_today))
        return raw

    stub_profile = briefing._ModelProfile(
        ollama_format_schema=briefing._gemma4_format_schema,
        plan_status_appendix=stub_appendix,
    )
    monkeypatch.setitem(briefing._MODEL_PROFILES, "ollama/gemma4", stub_profile)

    def fake_local_completion(system_prompt, user_prompt, **kw):
        return _gemma4_slot_json(
            _takeaway(headline="Workout"), _takeaway(headline="Steps"), [])

    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        fake_local_completion)
    _drain_model("opencode:ollama/gemma4", save=False)
    assert len(stub_calls) == 1
    assert stub_calls[0][1] == _PLAN_TODAY


def test_off_machine_warning_log_does_not_fire_for_ollama_dispatch(stream_env, monkeypatch, caplog):
    """generate_opencode_completion is structurally unreachable for
    provider=='ollama' models — so its off-machine warning log can never
    fire on that path. Asserted here (not in test_opencode_model.py, where
    it would be vacuous since that module's function is never called for
    Ollama models at all)."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        lambda *a, **kw: _brief_json([_takeaway()]))
    with caplog.at_level(logging.WARNING, logger="local_fitness.agent.opencode_model"):
        _drain_model("opencode:ollama/gemma4", save=False)
    assert not any("off-machine" in r.message for r in caplog.records)


# --- Plan-fold gap: gemma4 fabricates plan facts when asked to derive them
# itself (2026-07-06 manual testing: invented a wrong days-to-race count and
# inverted a "missed" verdict to "on track"). Fixed by pre-computing the
# facts in Python instead of asking the model to re-derive them. ----------

_PLAN_TODAY = {
    "goal_type": "half",
    "target_time_seconds": 6420,  # 1:47:00
    "days_to_race": 10,
    "last_graded": {"description": "20min tempo at half-marathon effort", "verdict": "missed"},
    "today": {"description": "Easy 5k, conversational pace"},
}


def test_format_race_goal_time():
    assert briefing._format_race_goal_time(6420) == "1:47:00"
    assert briefing._format_race_goal_time(154) == "2:34"
    assert briefing._format_race_goal_time(None) is None


def test_gemma4_plan_prompt_facts_cites_precomputed_values():
    text = briefing._gemma4_plan_prompt_facts(_PLAN_TODAY)
    assert "CITE THESE VERBATIM" in text
    assert "sub-1:47:00 half" in text
    assert "10 days out" in text
    assert "MISSED" in text
    assert "Easy 5k, conversational pace" in text


def test_gemma4_plan_prompt_facts_handles_no_graded_session_or_no_session_today():
    text = briefing._gemma4_plan_prompt_facts({
        "goal_type": "marathon", "target_time_seconds": None, "days_to_race": None,
        "last_graded": None, "today": None,
    })
    assert "do not invent a status" in text
    assert "no session scheduled today" in text


def test_gemma4_plan_status_appendix_is_correct_and_keyword_bearing():
    """The appendix must be both factually correct (computed, not generated)
    and hit ab_brief._PLAN_KEYWORDS reliably, since gemma4's own free-text
    phrasing was shown not to (2026-07-06 shadow-run: 0/4 runs matched)."""
    text = briefing._gemma4_plan_status_appendix(_PLAN_TODAY)
    assert "sub-1:47:00 half" in text
    assert "10 days to race day" in text
    assert "MISSED" in text
    assert 'the plan calls for: "Easy 5k, conversational pace"' in text
    # Matches ab_brief._PLAN_KEYWORDS's exact substrings.
    lowered = text.lower()
    for keyword in ("training plan", "race day", "adherence", "plan calls for"):
        assert keyword in lowered, keyword


def test_gemma4_plan_status_appendix_handles_no_active_plan_data():
    text = briefing._gemma4_plan_status_appendix({
        "goal_type": "marathon", "target_time_seconds": None, "days_to_race": None,
        "last_graded": None, "today": None,
    })
    assert "race day date not yet set" in text
    assert "session graded yet on the plan" in text
    assert "the plan calls for no session today" in text


def test_append_gemma4_plan_status_appends_to_workout_takeaway_details():
    raw = _brief_json([_takeaway(headline="Workout"), _takeaway(headline="Steps")])
    result = json.loads(briefing._append_gemma4_plan_status(raw, _PLAN_TODAY))
    assert "Training plan status" in result["takeaways"][0]["details"]
    assert "MISSED" in result["takeaways"][0]["details"]
    # Only the workout slot (index 0) is touched.
    assert "Training plan status" not in result["takeaways"][1]["details"]


def test_append_gemma4_plan_status_noop_when_no_active_plan():
    raw = _brief_json([_takeaway()])
    assert briefing._append_gemma4_plan_status(raw, None) == raw


def test_append_gemma4_plan_status_noop_on_malformed_or_empty_takeaways():
    assert briefing._append_gemma4_plan_status("not json", _PLAN_TODAY) == "not json"
    raw = json.dumps({"takeaways": []})
    assert briefing._append_gemma4_plan_status(raw, _PLAN_TODAY) == raw


def _fake_plan_context(plan_today: dict) -> BriefContext:
    return BriefContext(date=date_today(), user_name="Nate", candidates=[], plan_today=plan_today)


def test_local_model_gemma4_folds_plan_status_into_workout_takeaway(stream_env, monkeypatch):
    """End-to-end: an active plan flows through the gemma4 dispatch branch —
    the prompt gets the pre-computed facts block, and the final saved brief's
    workout takeaway carries the deterministic, correct plan-status
    appendix regardless of what the (here, faked) model returned."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setattr(briefing.brief_planner, "assemble_brief_context",
                        lambda **kw: _fake_plan_context(_PLAN_TODAY))
    calls: list[str] = []

    def fake_local_completion(system_prompt, user_prompt, **kw):
        calls.append(user_prompt)
        return _gemma4_slot_json(
            _takeaway(headline="Workout"), _takeaway(headline="Steps"), [])

    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        fake_local_completion)
    events = _drain_model("opencode:ollama/gemma4", save=True)

    assert "CITE THESE VERBATIM" in calls[0]
    assert "10 days out" in calls[0]
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    details = done[0]["brief"]["takeaways"][0]["details"]
    assert "Training plan status" in details
    assert "MISSED" in details


def test_local_model_gemma4_skips_plan_facts_when_no_active_plan(stream_env, monkeypatch):
    """No active plan → no prompt facts block, no appendix — the plan-fold
    machinery is a no-op path, not always-on."""
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", "1")
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(stream_env.parent / "notes.md"))
    monkeypatch.setattr(briefing.brief_planner, "assemble_brief_context",
                        lambda **kw: _fake_plan_context(None))
    calls: list[str] = []

    def fake_local_completion(system_prompt, user_prompt, **kw):
        calls.append(user_prompt)
        return _gemma4_slot_json(
            _takeaway(headline="Workout"), _takeaway(headline="Steps"), [])

    monkeypatch.setattr(briefing.local_model, "generate_local_completion",
                        fake_local_completion)
    events = _drain_model("opencode:ollama/gemma4", save=True)

    assert "CITE THESE VERBATIM" not in calls[0]
    done = [e for e in events if e["type"] == "done"]
    assert "Training plan status" not in done[0]["brief"]["takeaways"][0]["details"]
