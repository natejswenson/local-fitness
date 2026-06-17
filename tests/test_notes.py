"""Tests for notes.py — the durable user-preference store."""
from __future__ import annotations

import pytest

from local_fitness import notes


@pytest.fixture
def notes_path(tmp_path):
    return tmp_path / "user_notes.md"


def test_empty_when_missing(notes_path):
    assert notes.read_notes(notes_path) == []
    assert notes.render_for_prompt(notes_path) == ""


def test_append_and_read(notes_path):
    n = notes.append_note("Roast me when I'm slipping", path=notes_path)
    assert n.text == "Roast me when I'm slipping"
    got = notes.read_notes(notes_path)
    assert len(got) == 1
    assert got[0].text == "Roast me when I'm slipping"
    assert got[0].line == 0


def test_append_collapses_whitespace(notes_path):
    n = notes.append_note("lead   with\n the   workout", path=notes_path)
    assert n.text == "lead with the workout"


def test_append_empty_raises(notes_path):
    with pytest.raises(ValueError):
        notes.append_note("   \n  ", path=notes_path)


def test_append_truncates_long_note(notes_path):
    n = notes.append_note("x" * 900, path=notes_path)
    assert n.text.endswith("…")
    assert len(n.text) <= 801


def test_render_newest_first_with_line_index(notes_path):
    notes.append_note("first", path=notes_path)
    notes.append_note("second", path=notes_path)
    rendered = notes.render_for_prompt(notes_path)
    lines = rendered.splitlines()
    assert lines[0] == "[1] second"
    assert lines[1] == "[0] first"


def test_append_returns_real_line_index(notes_path):
    # The returned line must be the real index read_notes assigns, so a
    # client can immediately target the new note via update/delete.
    n0 = notes.append_note("first", path=notes_path)
    n1 = notes.append_note("second", path=notes_path)
    assert n0.line == 0
    assert n1.line == 1
    got = notes.read_notes(notes_path)
    assert got[n0.line].text == "first"
    assert got[n1.line].text == "second"
    # Deleting via the returned line removes exactly that note.
    assert notes.delete_note(n1.line, path=notes_path) is True
    remaining = notes.read_notes(notes_path)
    assert len(remaining) == 1
    assert remaining[0].text == "first"


def test_update_note(notes_path):
    notes.append_note("old pref", path=notes_path)
    updated = notes.update_note(0, "new pref", path=notes_path)
    assert updated is not None
    assert updated.text == "new pref"
    assert notes.read_notes(notes_path)[0].text == "new pref"


def test_update_note_bad_index(notes_path):
    notes.append_note("a", path=notes_path)
    assert notes.update_note(9, "x", path=notes_path) is None


def test_update_note_missing_file(notes_path):
    assert notes.update_note(0, "x", path=notes_path) is None


def test_update_note_empty_raises(notes_path):
    notes.append_note("a", path=notes_path)
    with pytest.raises(ValueError):
        notes.update_note(0, "   ", path=notes_path)


def test_delete_note(notes_path):
    notes.append_note("a", path=notes_path)
    notes.append_note("b", path=notes_path)
    assert notes.delete_note(0, path=notes_path) is True
    remaining = notes.read_notes(notes_path)
    assert len(remaining) == 1
    assert remaining[0].text == "b"


def test_delete_note_bad_index(notes_path):
    notes.append_note("a", path=notes_path)
    assert notes.delete_note(5, path=notes_path) is False


def test_delete_note_missing_file(notes_path):
    assert notes.delete_note(0, path=notes_path) is False


def test_parse_tolerates_non_bullets(notes_path):
    notes_path.write_text("# a heading\n- 2026-06-01T00:00:00 — real note\nfree prose\n")
    got = notes.read_notes(notes_path)
    assert len(got) == 1
    assert got[0].text == "real note"


def test_parse_hyphen_separator_fallback(notes_path):
    notes_path.write_text("- 2026-06-01T00:00:00 - hand edited\n")
    got = notes.read_notes(notes_path)
    assert got[0].text == "hand edited"


def test_parse_undated_bullet(notes_path):
    notes_path.write_text("- just text no separator\n")
    got = notes.read_notes(notes_path)
    assert got[0].text == "just text no separator"
    assert got[0].timestamp == ""


def test_rotation_to_archive(notes_path):
    # Drive past the 4 KB live cap so rotation + archiving fires.
    for i in range(120):
        notes.append_note(f"preference number {i} with some padding text here", path=notes_path)
    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) <= notes.LIVE_FILE_MAX_BYTES
    archive = notes._archive_path(notes_path)
    assert archive.exists()
    assert archive.read_text(encoding="utf-8").strip()


def test_default_notes_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "custom.md"))
    assert notes._default_notes_path() == tmp_path / "custom.md"
    monkeypatch.delenv("LOCAL_FITNESS_NOTES_PATH")
    monkeypatch.setenv("LOCAL_FITNESS_DATA_DIR", str(tmp_path))
    assert notes._default_notes_path() == tmp_path / "user_notes.md"
