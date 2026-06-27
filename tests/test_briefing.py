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
from local_fitness.agent import briefing
from local_fitness.agent import briefs


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

def test_brief_v2_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_V2", raising=False)
    assert briefing._brief_v2_enabled() is False


@pytest.mark.parametrize("val,on", [("1", True), ("true", True), ("on", True),
                                    ("YES", True), ("0", False), ("off", False),
                                    ("", False), ("maybe", False)])
def test_brief_v2_flag_parsing(monkeypatch, val, on):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_V2", val)
    assert briefing._brief_v2_enabled() is on


def test_v1_default_routes_tools(stream_env, monkeypatch):
    """Flag OFF (default) → the V1 monolith: MCP server attached, max_turns=20,
    Step-1 tool orchestration in the prompt."""
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_V2", raising=False)
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
    with caplog.at_level(logging.INFO, logger="local_fitness.agent.briefing"):
        events = _drain(save=True)
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert any("brief_grounding" in r.message and "invention_rate" in r.message
               for r in caplog.records)
    # The advisory signal must not have changed the saved brief.
    assert done[0]["brief"]["takeaways"][0]["headline"] == _TAKEAWAY["headline"]


def test_v1_path_does_not_run_grounding(stream_env, monkeypatch, caplog):
    """V1 has no BriefContext → no grounding log (nothing to ground against)."""
    import logging
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_V2", raising=False)
    _install_query(monkeypatch, [_text_event(_brief_json([_takeaway()]))])
    with caplog.at_level(logging.INFO, logger="local_fitness.agent.briefing"):
        _drain(save=True)
    assert not any("brief_grounding" in r.message for r in caplog.records)
