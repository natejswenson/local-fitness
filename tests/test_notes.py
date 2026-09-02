"""Tests for notes.py — the durable user-preference store."""
from __future__ import annotations

import stat
import threading

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
    assert got[0].position == 0
    assert got[0].handle == n.handle
    assert len(n.handle) == 8


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


def test_render_newest_first_with_handle(notes_path):
    n0 = notes.append_note("first", path=notes_path)
    n1 = notes.append_note("second", path=notes_path)
    rendered = notes.render_for_prompt(notes_path)
    lines = rendered.splitlines()
    today = n1.timestamp[:10]
    assert lines[0] == f"[{n1.handle}] {today} — second"
    assert lines[1] == f"[{n0.handle}] {today} — first"


def test_append_returns_a_handle_that_resolves_immediately(notes_path):
    # The returned handle must resolve against read_notes/update/delete
    # right away, so a client can act on the note it just wrote.
    n0 = notes.append_note("first", path=notes_path)
    n1 = notes.append_note("second", path=notes_path)
    got = {n.handle: n for n in notes.read_notes(notes_path)}
    assert got[n0.handle].text == "first"
    assert got[n1.handle].text == "second"
    # Deleting via the returned handle removes exactly that note.
    assert notes.delete_note(n1.handle, path=notes_path) == 1
    remaining = notes.read_notes(notes_path)
    assert len(remaining) == 1
    assert remaining[0].text == "first"


def test_update_note(notes_path):
    n0 = notes.append_note("old pref", path=notes_path)
    result = notes.update_note(n0.handle, "new pref", path=notes_path)
    assert result is not None
    updated, duplicates = result
    assert updated.text == "new pref"
    assert duplicates == 1
    assert notes.read_notes(notes_path)[0].text == "new pref"


def test_update_note_bad_handle(notes_path):
    notes.append_note("a", path=notes_path)
    assert notes.update_note("deadbeef", "x", path=notes_path) is None


def test_update_note_stale_handle_after_the_note_it_named_changed(notes_path):
    # The compare-and-swap: once the target itself has been rewritten, its
    # old handle must refuse rather than silently landing on the new text.
    n0 = notes.append_note("original", path=notes_path)
    notes.update_note(n0.handle, "rewritten", path=notes_path)
    assert notes.update_note(n0.handle, "should not land", path=notes_path) is None
    assert notes.read_notes(notes_path)[0].text == "rewritten"


def test_update_note_handle_is_normalised(notes_path):
    n0 = notes.append_note("old pref", path=notes_path)
    bracketed = f"  [{n0.handle.upper()}]  "
    result = notes.update_note(bracketed, "new pref", path=notes_path)
    assert result is not None
    assert result[0].text == "new pref"


def test_update_note_missing_file(notes_path):
    assert notes.update_note("deadbeef", "x", path=notes_path) is None


def test_update_note_empty_raises(notes_path):
    n0 = notes.append_note("a", path=notes_path)
    with pytest.raises(ValueError):
        notes.update_note(n0.handle, "   ", path=notes_path)


def test_delete_note(notes_path):
    n0 = notes.append_note("a", path=notes_path)
    notes.append_note("b", path=notes_path)
    assert notes.delete_note(n0.handle, path=notes_path) == 1
    remaining = notes.read_notes(notes_path)
    assert len(remaining) == 1
    assert remaining[0].text == "b"


def test_delete_note_bad_handle(notes_path):
    notes.append_note("a", path=notes_path)
    assert notes.delete_note("deadbeef", path=notes_path) is None


def test_delete_note_missing_file(notes_path):
    assert notes.delete_note("deadbeef", path=notes_path) is None


def test_a_delete_no_longer_redirects_a_later_update_by_handle(notes_path):
    # Repro 1: four notes; delete the second by handle; the third — targeted
    # by the handle captured BEFORE the delete — must still be the one that
    # changes, and the fourth must survive untouched. On dev (line-index
    # addressing) this silently rewrote the fourth note instead.
    handles = [notes.append_note(t, path=notes_path).handle
               for t in ("zero", "one", "two", "three")]
    assert notes.delete_note(handles[1], path=notes_path) == 1
    result = notes.update_note(handles[2], "REWRITTEN", path=notes_path)
    assert result is not None
    texts = {n.text for n in notes.read_notes(notes_path)}
    assert texts == {"zero", "REWRITTEN", "three"}


def test_rotation_does_not_redirect_a_delete_of_a_note_that_stays_live(notes_path):
    # Repro 2, live variant: after enough notes to be near the cap, a
    # handle captured for the newest of them must still resolve to that
    # SAME note once one more append trips rotation — rotation evicts by
    # position from the oldest end, so the newest is never a candidate.
    handles = [
        notes.append_note(f"note number {i:03d} padding text here", path=notes_path).handle
        for i in range(60)
    ]
    target = handles[-1]
    target_text = notes.read_notes(notes_path)[-1].text
    notes.append_note("x" * 700, path=notes_path)  # forces rotation
    assert any(n.handle == target for n in notes.read_notes(notes_path))
    assert notes.delete_note(target, path=notes_path) == 1
    assert all(n.text != target_text for n in notes.read_notes(notes_path))


def test_rotation_refuses_a_delete_of_a_note_that_was_itself_rotated(notes_path):
    # Repro 2, rotated variant: a handle captured for one of the OLDEST
    # notes is exactly what a position-based rotation evicts first. On
    # dev this silently deleted whatever note now occupies that stale
    # line index and reported success; here it must refuse loudly and
    # leave the live file byte-identical.
    handles = [
        notes.append_note(f"preference number {i} with some padding text here", path=notes_path).handle
        for i in range(120)
    ]
    target = handles[5]
    assert not any(n.handle == target for n in notes.read_notes(notes_path))  # rotated out
    before = notes_path.read_text(encoding="utf-8")
    assert notes.delete_note(target, path=notes_path) is None
    assert notes_path.read_text(encoding="utf-8") == before


def test_duplicate_handle_stays_addressable_and_converges_on_update(notes_path):
    notes_path.write_text(
        "- 2026-01-01T00:00:00 — same text twice\n"
        "- 2026-01-01T00:00:00 — same text twice\n"
        "- 2026-01-02T00:00:00 — unrelated\n",
        encoding="utf-8",
    )
    dup_handle = next(n.handle for n in notes.read_notes(notes_path)
                       if n.text == "same text twice")
    result = notes.update_note(dup_handle, "now distinct", path=notes_path)
    assert result is not None
    _, duplicates = result
    assert duplicates == 2
    texts = [n.text for n in notes.read_notes(notes_path)]
    assert texts.count("same text twice") == 1
    assert texts.count("now distinct") == 1
    remaining = notes.read_notes(notes_path)
    handles = [n.handle for n in remaining]
    assert len(handles) == len(set(handles))  # every note now uniquely addressable


def test_duplicate_handle_stays_addressable_and_converges_on_delete(notes_path):
    notes_path.write_text(
        "- 2026-01-01T00:00:00 — same text twice\n"
        "- 2026-01-01T00:00:00 — same text twice\n",
        encoding="utf-8",
    )
    dup_handle = notes.read_notes(notes_path)[0].handle
    assert notes.delete_note(dup_handle, path=notes_path) == 2
    remaining = notes.read_notes(notes_path)
    assert len(remaining) == 1
    assert remaining[0].text == "same text twice"
    # The survivor is addressable on its own right after.
    assert notes.delete_note(remaining[0].handle, path=notes_path) == 1
    assert notes.read_notes(notes_path) == []


def test_append_never_manufactures_a_handle_collision(notes_path):
    # Two saves with identical text inside the same wall-clock second must
    # not collide — append_note re-stamps a second later instead.
    n0 = notes.append_note("same text", path=notes_path)
    n1 = notes.append_note("same text", path=notes_path)
    assert n0.handle != n1.handle
    assert len({n.handle for n in notes.read_notes(notes_path)}) == 2


def test_render_for_prompt_handles_all_resolve_via_update(notes_path):
    # Correction 2: the prompt path is a second live entry point — every
    # handle rendered into the prompt must be one list_user_notes would
    # also hand out, and each must resolve through update_note.
    for t in ("alpha", "beta", "gamma"):
        notes.append_note(t, path=notes_path)
    rendered_handles = {
        line.split("]", 1)[0].lstrip("[")
        for line in notes.render_for_prompt(notes_path).splitlines()
    }
    listed_handles = {n.handle for n in notes.read_notes(notes_path)}
    assert rendered_handles == listed_handles
    for h in rendered_handles:
        result = notes.update_note(h, "touched", path=notes_path)
        assert result is not None


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


def test_append_after_rotation_repairs_missing_trailing_newline(notes_path):
    # Simulate a hand-edited live file with no trailing newline — the
    # module docstring explicitly invites hand-editing — sized so the
    # next append crosses the 4 KB cap and rotation fires. On dev,
    # _rotate_to_fit's "".join(lines) + new_line welds the previously-last
    # (no-newline) line directly into the incoming bullet instead of
    # keeping them as two distinct notes (Repro 4).
    original_texts = [
        f"note number {i:03d} with some padding text to bulk it up" for i in range(60)
    ]
    bullets = [f"- 2026-01-01T00:00:{i:02d} — {t}" for i, t in enumerate(original_texts)]
    notes_path.write_text("\n".join(bullets), encoding="utf-8")  # no trailing newline

    new_note = notes.append_note("Never comment on my weekend sleep.", path=notes_path)
    assert new_note.text == "Never comment on my weekend sleep."

    archive = notes._archive_path(notes_path)
    assert archive.exists()  # rotation must actually have fired, or this proves nothing

    live_texts = [n.text for n in notes.read_notes(notes_path)]
    archived_texts = [
        parsed.text
        for parsed in (notes._parse_line(ln) for ln in archive.read_text(encoding="utf-8").splitlines())
        if parsed is not None
    ]
    all_texts = archived_texts + live_texts

    # Every original note, plus the new one, must survive as its own
    # distinct entry -- none merged with its neighbour, none lost.
    assert all_texts.count("Never comment on my weekend sleep.") == 1
    for text in original_texts:
        assert all_texts.count(text) == 1
    assert len(all_texts) == len(original_texts) + 1


def test_append_archive_repairs_missing_trailing_newline_in_existing_archive(notes_path):
    # Same defect, second location: _append_archive guarded the newline of
    # the text it *writes* but not of the file it appends *to*. A
    # hand-edited (or previously-welded) archive with no trailing newline
    # would weld the first newly-rotated note onto its last existing line.
    archive = notes._archive_path(notes_path)
    archive.write_text("- 2025-01-01T00:00:00 — an old archived preference", encoding="utf-8")

    original_texts = [
        f"note number {i:03d} with some padding text to bulk it up" for i in range(60)
    ]
    bullets = [f"- 2026-01-01T00:00:{i:02d} — {t}" for i, t in enumerate(original_texts)]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")

    notes.append_note("a rotated preference", path=notes_path)

    archived_texts = [
        parsed.text
        for parsed in (notes._parse_line(ln) for ln in archive.read_text(encoding="utf-8").splitlines())
        if parsed is not None
    ]
    assert "an old archived preference" in archived_texts
    assert len(archived_texts) >= 2
    # The pre-existing entry must be its own note, not a prefix glued to
    # whatever rotated in behind it.
    assert archived_texts.count("an old archived preference") == 1


def test_locked_rewrite_no_lost_write_across_concurrent_append(notes_path):
    # A second writer, forced to overlap this held lock via the test
    # seam, must not be lost: it can only acquire the sidecar lock once
    # this critical section releases it, so it reads the file this
    # write just produced rather than a stale snapshot.
    first_handle = notes.append_note("first", path=notes_path).handle
    thread_box: dict[str, threading.Thread] = {}

    def hook():
        t = threading.Thread(
            target=notes.append_note,
            args=("second, concurrent",),
            kwargs={"path": notes_path},
        )
        t.start()
        thread_box["thread"] = t

    with notes._locked_rewrite(notes_path, _after_read_hook=hook) as ctx:
        match = ctx.resolve(first_handle)
        assert match is not None
        lines = ctx.lines
        lines[match.index] = "- 2026-01-01T00:00:00 — first, updated\n"
        ctx.text = "".join(lines)

    thread_box["thread"].join(timeout=5)
    assert not thread_box["thread"].is_alive()

    remaining = {n.text for n in notes.read_notes(notes_path)}
    assert remaining == {"first, updated", "second, concurrent"}


def test_locked_rewrite_loses_a_write_when_the_lock_is_a_no_op(notes_path, monkeypatch):
    # Two-sided: stub the lock acquisition to a no-op and force the exact
    # same interleaving — the second writer now runs to completion
    # (nothing blocks it) before this write lands, and gets clobbered.
    # Proves the lock above is load-bearing, not incidental timing.
    first_handle = notes.append_note("first", path=notes_path).handle
    monkeypatch.setattr(notes.fcntl, "flock", lambda *a, **k: None)

    def hook():
        t = threading.Thread(
            target=notes.append_note,
            args=("second, concurrent",),
            kwargs={"path": notes_path},
        )
        t.start()
        # No real lock blocks it, so this completes fast and
        # deterministically — no sleep needed to force the race.
        t.join(timeout=5)

    with notes._locked_rewrite(notes_path, _after_read_hook=hook) as ctx:
        match = ctx.resolve(first_handle)
        assert match is not None
        lines = ctx.lines
        lines[match.index] = "- 2026-01-01T00:00:00 — first, updated\n"
        ctx.text = "".join(lines)

    remaining = {n.text for n in notes.read_notes(notes_path)}
    assert remaining == {"first, updated"}
    assert "second, concurrent" not in remaining


def test_read_notes_never_sees_a_partial_file(notes_path):
    # On dev (truncate-then-write through one handle) this observes 0
    # notes on a meaningful fraction of reads. Atomic replace closes
    # that window: every read sees the full file or the file before it.
    for i in range(20):
        notes.append_note(f"seed note {i}", path=notes_path)

    stop = threading.Event()

    def writer():
        i = 0
        current = notes.read_notes(notes_path)[0].handle
        while not stop.is_set():
            result = notes.update_note(current, f"updated {i}", path=notes_path)
            if result is not None:
                current = result[0].handle
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        counts = [len(notes.read_notes(notes_path)) for _ in range(1000)]
    finally:
        stop.set()
        t.join(timeout=5)

    assert all(c == 20 for c in counts)


def test_atomic_replace_preserves_file_mode(notes_path):
    notes.append_note("first", path=notes_path)
    notes_path.chmod(0o644)
    notes.append_note("second", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o644


def test_atomic_replace_default_mode_for_a_new_file(notes_path, monkeypatch):
    monkeypatch.setattr(notes, "_current_umask", lambda: 0o022)
    notes.append_note("first", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o644


def test_default_notes_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "custom.md"))
    assert notes._default_notes_path() == tmp_path / "custom.md"
    monkeypatch.delenv("LOCAL_FITNESS_NOTES_PATH")
    monkeypatch.setenv("LOCAL_FITNESS_DATA_DIR", str(tmp_path))
    assert notes._default_notes_path() == tmp_path / "user_notes.md"
