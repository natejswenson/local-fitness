"""Tests for agent/briefs.py — the Claude-free brief read/write/salvage gate.

``briefs.save_brief`` is the single validate + atomic-write gate; ``load_today``
and ``load_latest`` are the only readers. Every test points
``briefs.DEFAULT_BRIEFINGS_DIR`` (and the DB) at a tmp dir so the real dev
briefings/ and DB are never touched.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from local_fitness import db
from local_fitness.agent import briefs
from local_fitness.agent.schemas import Brief


def _valid_takeaway(**over) -> dict:
    tk = {
        "headline": "Easy 5k on tap",
        "summary": "RHR is steady and TSB is positive — green light to run.",
        "tone": "positive",
        "details": "Full markdown deep-dive goes here.",
    }
    tk.update(over)
    return tk


@pytest.fixture
def briefs_dir(tmp_path, monkeypatch):
    """Point briefs I/O + the DB at a tmp dir. Returns the briefings dir."""
    out = tmp_path / "briefings"
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", out)
    dbp = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", dbp)
    db.init_schema(dbp)
    return out


# --- save_brief: happy path ------------------------------------------------

def test_save_brief_valid_payload_returns_and_writes(briefs_dir):
    result = briefs.save_brief({"takeaways": [_valid_takeaway()]})
    today = date.today().isoformat()

    assert result["saved"] is True
    assert result["date"] == today
    assert set(result.keys()) == {"saved", "date", "path", "brief"}
    assert isinstance(result["brief"], Brief)

    written = briefs_dir / f"{today}.json"
    assert written.exists()
    assert str(written) == result["path"]

    brief = result["brief"]
    assert brief.date == today
    # generated_at is stamped to a parseable now-timestamp (today's date).
    assert brief.generated_at is not None
    assert datetime.fromisoformat(brief.generated_at).date() == date.today()
    # user_name defaulted (no setting stored) to the project default.
    assert brief.user_name == briefs.DEFAULT_USER_NAME


def test_save_brief_honors_stored_user_name(briefs_dir):
    db.set_setting("user_name", "Nate")
    result = briefs.save_brief({"takeaways": [_valid_takeaway()]})
    assert result["brief"].user_name == "Nate"


# --- save_brief: server-side date stamp wins -------------------------------

def test_save_brief_forces_today_over_payload_date(briefs_dir):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    result = briefs.save_brief(
        {"date": yesterday, "takeaways": [_valid_takeaway()]}
    )
    today = date.today().isoformat()

    # The stamped (today) date wins over the payload's date.
    assert result["date"] == today
    assert result["brief"].date == today
    # File is today's, NOT the payload's yesterday.
    assert (briefs_dir / f"{today}.json").exists()
    assert not (briefs_dir / f"{yesterday}.json").exists()


def test_save_brief_forces_generated_at_over_payload(briefs_dir):
    stale = "2000-01-01T00:00:00"
    result = briefs.save_brief(
        {"generated_at": stale, "takeaways": [_valid_takeaway()]}
    )
    assert result["brief"].generated_at != stale
    assert datetime.fromisoformat(result["brief"].generated_at).date() == date.today()


# --- save_brief: invalid payloads raise + write nothing --------------------

def _assert_rejected(briefs_dir, payload):
    with pytest.raises(ValidationError):
        briefs.save_brief(payload)
    # No file written on rejection (rules out a partial/atomic-tmp leak too).
    assert list(briefs_dir.glob("*.json")) == []
    assert list(briefs_dir.glob(".*.tmp")) == []


def test_save_brief_rejects_empty_takeaways(briefs_dir):
    _assert_rejected(briefs_dir, {"takeaways": []})


def test_save_brief_rejects_too_many_takeaways(briefs_dir):
    _assert_rejected(briefs_dir, {"takeaways": [_valid_takeaway() for _ in range(6)]})


def test_save_brief_rejects_bad_tone(briefs_dir):
    _assert_rejected(briefs_dir, {"takeaways": [_valid_takeaway(tone="ecstatic")]})


def test_save_brief_rejects_bad_metric_name(briefs_dir):
    bad = _valid_takeaway(metric={"metric": "not_a_metric", "days": 14})
    _assert_rejected(briefs_dir, {"takeaways": [bad]})


# --- save_brief: salvage paths ---------------------------------------------

def test_save_brief_salvages_json_string_with_fences(briefs_dir):
    """A raw JSON STRING wrapped in a ```json fence with a stray control char
    is repaired by the _extract_json path and persisted."""
    payload = (
        '```json\n{"takeaways": [{"headline": "Run logged\x07",'
        ' "summary": "Second straight day.", "tone": "positive",'
        ' "details": "Nice."}]}\n```'
    )
    result = briefs.save_brief(payload)
    assert result["saved"] is True
    # Control char stripped from the headline.
    assert result["brief"].takeaways[0].headline == "Run logged"
    assert (briefs_dir / f"{date.today().isoformat()}.json").exists()


def test_save_brief_salvages_nested_takeaways_dict(briefs_dir):
    """A dict that buries the takeaways under a sibling key (a user note can
    convince the model to wrap them) is recovered by _salvage_takeaways."""
    payload = {"snapshot": {"foo": 1}, "wrapper": {"takeaways": [_valid_takeaway()]}}
    result = briefs.save_brief(payload)
    assert result["saved"] is True
    assert len(result["brief"].takeaways) == 1
    assert result["brief"].takeaways[0].headline == _valid_takeaway()["headline"]
    assert (briefs_dir / f"{date.today().isoformat()}.json").exists()


# --- atomic write: file round-trips to an equal Brief ----------------------

def test_save_brief_atomic_write_reads_back_equal(briefs_dir):
    result = briefs.save_brief({"takeaways": [_valid_takeaway()]})
    # No leftover tmp file; final file is complete valid JSON.
    assert list(briefs_dir.glob(".*.tmp")) == []
    loaded = briefs.load_today()
    assert loaded == result["brief"]
    assert briefs.load_latest() == result["brief"]


# --- load_today ------------------------------------------------------------

def test_load_today_present_and_absent(briefs_dir):
    assert briefs.load_today() is None  # nothing on disk yet
    saved = briefs.save_brief({"takeaways": [_valid_takeaway()]})
    got = briefs.load_today()
    assert got is not None
    assert got.date == date.today().isoformat()
    assert got == saved["brief"]


# --- load_latest -----------------------------------------------------------

def test_load_latest_none_on_missing_dir(briefs_dir):
    # briefs_dir is not created until a save; load_latest must not raise.
    assert briefs.load_latest() is None


def _write_raw_brief(out_dir, d: str, headline: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = Brief(
        date=d,
        user_name="tester",
        generated_at=f"{d}T08:00:00",
        takeaways=[Brief.model_validate(
            {"date": d, "user_name": "t",
             "takeaways": [_valid_takeaway(headline=headline)]}
        ).takeaways[0]],
    )
    (out_dir / f"{d}.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")


def test_load_latest_picks_most_recent_by_filename(briefs_dir):
    d1 = "2026-06-10"
    d2 = "2026-06-12"
    d3 = "2026-06-11"
    _write_raw_brief(briefs_dir, d1, "oldest")
    _write_raw_brief(briefs_dir, d2, "newest")
    _write_raw_brief(briefs_dir, d3, "middle")
    latest = briefs.load_latest()
    assert latest is not None
    assert latest.date == d2
    assert latest.takeaways[0].headline == "newest"


def test_load_latest_skips_unparseable_file(briefs_dir):
    good = "2026-06-10"
    _write_raw_brief(briefs_dir, good, "good")
    # A later-dated file that's NOT valid JSON must be skipped, falling back
    # to the next most recent parseable one.
    (briefs_dir / "2026-06-13.json").write_text("{not valid json", encoding="utf-8")
    latest = briefs.load_latest()
    assert latest is not None
    assert latest.date == good


# --- _recent_briefs_summary ------------------------------------------------

def test_recent_briefs_summary_renders_recent_days(briefs_dir, monkeypatch):
    anchor = date(2026, 6, 16)
    y = (anchor - timedelta(days=1)).isoformat()
    _write_raw_brief(briefs_dir, y, "Run logged yesterday")
    summary = briefs._recent_briefs_summary(today=anchor)
    assert y in summary
    assert "Run logged yesterday" in summary


def test_recent_briefs_summary_empty_when_no_history(briefs_dir):
    assert briefs._recent_briefs_summary(today=date(2026, 6, 16)) == ""


def _write_raw_json(out_dir, d: str, text: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{d}.json").write_text(text, encoding="utf-8")


def test_recent_briefs_summary_skips_unparseable_and_emptyish(briefs_dir):
    """The lookback loop must survive a corrupt file, a takeaway-less brief,
    and a takeaway with no headline — all three are skipped, leaving only the
    one good headline rendered."""
    # The briefs_dir fixture already points DEFAULT_BRIEFINGS_DIR at this dir.
    anchor = date(2026, 6, 16)
    d1 = (anchor - timedelta(days=1)).isoformat()
    d2 = (anchor - timedelta(days=2)).isoformat()
    d3 = (anchor - timedelta(days=3)).isoformat()
    d4 = (anchor - timedelta(days=4)).isoformat()

    _write_raw_json(briefs_dir, d1, "{ not valid json")          # 289-290 skip
    _write_raw_json(briefs_dir, d2, '{"takeaways": []}')          # 293 skip (no takeaways)
    _write_raw_json(
        briefs_dir, d3,
        '{"takeaways": [{"headline": "", "tone": "neutral"}]}',   # 300 skip (no headline)
    )
    _write_raw_json(
        briefs_dir, d4,
        '{"takeaways": [{"headline": "Good run", "tone": "positive",'
        ' "summary": "Solid effort."}]}',
    )

    summary = briefs._recent_briefs_summary(today=anchor)
    assert "Good run" in summary
    assert "Solid effort." in summary
    # The empty-headline takeaway produced no bullet line.
    assert "- [" in summary
    assert summary.count("- [") == 1


# --- _default_briefings_dir env override -----------------------------------

def test_default_briefings_dir_honors_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-briefings"
    monkeypatch.setenv("LOCAL_FITNESS_BRIEFINGS_DIR", str(override))
    assert briefs._default_briefings_dir() == override


# --- _strip_inline_control_chars: escape handling --------------------------

def test_strip_keeps_valid_json_escape():
    # A legit \n escape is copied verbatim (both chars survive).
    assert briefs._strip_inline_control_chars('"a\\nb"') == '"a\\nb"'


def test_strip_drops_invalid_escape_keeps_char():
    # \| is not a JSON escape — drop the backslash, keep the pipe.
    assert briefs._strip_inline_control_chars('"a\\|b"') == '"a|b"'


def test_strip_drops_invalid_escape_of_control_char():
    # Backslash followed by a raw control char: drop both.
    assert briefs._strip_inline_control_chars('"a\\\x01b"') == '"ab"'


def test_strip_drops_trailing_backslash():
    # A string that ends mid-escape (backslash with nothing after) drops it.
    assert briefs._strip_inline_control_chars('"abc\\') == '"abc'


# --- _fix_numeric_gaps_outside_strings: escape + unterminated --------------

def test_fix_numeric_gaps_preserves_escaped_chars_in_string():
    # Escaped quote inside a string must not terminate it; numbers with
    # spaces inside the string prose are left untouched.
    src = '{"note": "a \\" 1 2 b"}'
    assert briefs._fix_numeric_gaps_outside_strings(src) == src


def test_fix_numeric_gaps_handles_unterminated_string():
    # An unterminated trailing string must flush without raising.
    src = '{"x": "open and never closed'
    assert briefs._fix_numeric_gaps_outside_strings(src) == src


def test_fix_numeric_gaps_collapses_outside_strings():
    # Whitespace between digits outside a string is collapsed.
    assert briefs._fix_numeric_gaps_outside_strings("1 2") == "12"


# --- _salvage_takeaways: branch coverage -----------------------------------

def test_salvage_non_dict_returned_unchanged():
    assert briefs._salvage_takeaways(["not", "a", "dict"]) == ["not", "a", "dict"]


def test_salvage_finds_nested_and_stops_at_first(monkeypatch):
    # Takeaways buried two structures deep, with sibling values after the
    # match (exercises the early-return once `found` is set) and a list node
    # that must be walked.
    payload = {
        "junk_list": [{"nope": 1}],
        "wrapper": {"takeaways": [_valid_takeaway(headline="first")]},
        "more": {"takeaways": [_valid_takeaway(headline="second")]},
    }
    salvaged = briefs._salvage_takeaways(payload)
    assert [tk["headline"] for tk in salvaged["takeaways"]] == ["first"]


def test_salvage_preserves_compatible_metadata():
    payload = {
        "date": "2026-06-01",
        "user_name": "Nate",
        "generated_at": "2026-06-01T08:00:00",
        "ignored_int": 7,  # non-str metadata is dropped
        "wrapper": {"takeaways": [_valid_takeaway()]},
    }
    salvaged = briefs._salvage_takeaways(payload)
    assert salvaged["date"] == "2026-06-01"
    assert salvaged["user_name"] == "Nate"
    assert salvaged["generated_at"] == "2026-06-01T08:00:00"
    assert "ignored_int" not in salvaged


def test_salvage_nothing_recognizable_returns_payload():
    payload = {"foo": "bar", "nested": {"baz": [1, 2, 3]}}
    assert briefs._salvage_takeaways(payload) == payload


# --- _extract_json: fence, bracket-scan, and raise paths -------------------

def test_extract_json_no_json_raises():
    with pytest.raises(ValueError, match="no JSON found"):
        briefs._extract_json("just prose, no object here")


def test_extract_json_bracket_scan_recovers_object():
    # Direct decode fails on the leading prose; no fence; the first-{ to
    # last-} bracket scan recovers the object.
    text = 'Here you go: {"takeaways": [{"headline": "x"}]} thanks!'
    out = briefs._extract_json(text)
    assert out["takeaways"][0]["headline"] == "x"


def test_extract_json_bracket_scan_failure_raises():
    # Braces present but the contents are not JSON → the bracket-scan branch
    # raises a descriptive ValueError.
    with pytest.raises(ValueError, match="could not parse JSON"):
        briefs._extract_json("prefix { not : valid json , } suffix")


def test_extract_json_fence_unparseable_falls_through_to_raise():
    # A ```json fence whose contents don't parse falls through; the bracket
    # scan then also fails on the same garbage → ValueError.
    text = "```json\n{ broken : json }\n```"
    with pytest.raises(ValueError, match="could not parse JSON"):
        briefs._extract_json(text)


# --- load_latest: all-unparseable returns None -----------------------------

def test_load_latest_returns_none_when_all_unparseable(briefs_dir):
    _write_raw_json(briefs_dir, "2026-06-10", "{garbage")
    _write_raw_json(briefs_dir, "2026-06-11", "also not json")
    assert briefs.load_latest() is None
