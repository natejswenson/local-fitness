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


def test_locked_rewrite_no_lost_write_across_concurrent_append(notes_path, monkeypatch):
    # Proof 9 (positive half). Per the design, this guards the refactor
    # rather than reproducing a live failure: a literal revert of
    # notes.py cannot show this red, because pre-fix update_note already
    # holds one flock across its own read+write, so it is ALSO safe from
    # this exact race — the design says as much ("passes on dev today").
    # The real regression this guards is a future rewrite of update_note
    # or delete_note that splits reading from writing without going
    # through _locked_rewrite; its two-sidedness is the paired no-op-lock
    # variant below, not a pre/post-fix comparison.
    #
    # The held side is a PRODUCTION writer (update_note), not a
    # hand-rolled _locked_rewrite block — a hand-rolled block can go
    # green forever even if update_note itself later stops using the
    # lock. The seam: update_note calls the module-global
    # notes._locked_rewrite, so wrapping that global injects the hook
    # into the one call this test needs to observe, and delegates every
    # other call (including the concurrent append's own) straight
    # through unchanged.
    notes.append_note("first", path=notes_path)
    thread_box: dict[str, threading.Thread] = {}
    real_locked_rewrite = notes._locked_rewrite
    real_flock = notes.fcntl.flock
    reached_lock = threading.Event()
    hooked = {"fired": False}

    def flock_signalling(*a, **k):
        # Fires only for the concurrent writer's own lock attempt (see
        # below: only installed once the held writer already has the
        # lock). Proves genuine overlap — the second call really is
        # contending for the same lock, not just running afterward.
        reached_lock.set()
        return real_flock(*a, **k)

    def wrapped(path, *, _after_read_hook=None):
        if hooked["fired"]:
            return real_locked_rewrite(path, _after_read_hook=_after_read_hook)
        hooked["fired"] = True

        def hook():
            monkeypatch.setattr(notes.fcntl, "flock", flock_signalling)
            t = threading.Thread(
                target=notes.append_note,
                args=("second, concurrent",),
                kwargs={"path": notes_path},
            )
            t.start()
            thread_box["thread"] = t
            assert reached_lock.wait(timeout=5), (
                "the concurrent append never reached its own flock() call"
            )

        return real_locked_rewrite(path, _after_read_hook=hook)

    monkeypatch.setattr(notes, "_locked_rewrite", wrapped)

    notes.update_note(0, "first, updated", path=notes_path)

    thread_box["thread"].join(timeout=5)
    assert not thread_box["thread"].is_alive()

    remaining = {n.text for n in notes.read_notes(notes_path)}
    assert remaining == {"first, updated", "second, concurrent"}


def test_locked_rewrite_loses_a_write_when_the_lock_is_a_no_op(notes_path, monkeypatch):
    # Two-sided pair for the test above: stub the lock acquisition to a
    # no-op and force the identical interleaving — the second writer now
    # runs to completion (nothing blocks it) before the held write
    # lands, and gets clobbered. Proves the lock above is load-bearing,
    # not incidental timing. Same production-writer seam as above:
    # update_note is the held side, append_note is the concurrent side.
    notes.append_note("first", path=notes_path)
    monkeypatch.setattr(notes.fcntl, "flock", lambda *a, **k: None)
    real_locked_rewrite = notes._locked_rewrite
    hooked = {"fired": False}
    errors: list[BaseException] = []
    landed = threading.Event()

    def append_second():
        try:
            notes.append_note("second, concurrent", path=notes_path)
        except BaseException as exc:  # noqa: BLE001 - must be observed, not swallowed
            errors.append(exc)
        finally:
            landed.set()

    def wrapped(path, *, _after_read_hook=None):
        if hooked["fired"]:
            return real_locked_rewrite(path, _after_read_hook=_after_read_hook)
        hooked["fired"] = True

        def hook():
            t = threading.Thread(target=append_second)
            t.start()
            # No real lock blocks it, so this completes fast and
            # deterministically — no sleep needed to force the race.
            t.join(timeout=5)

        return real_locked_rewrite(path, _after_read_hook=hook)

    monkeypatch.setattr(notes, "_locked_rewrite", wrapped)

    notes.update_note(0, "first, updated", path=notes_path)

    # The concurrent append must have actually completed (not crashed
    # silently) for its disappearance below to demonstrate real data
    # loss rather than a write that never happened.
    assert landed.wait(timeout=5), "the concurrent append never completed"
    assert not errors, f"the concurrent append raised: {errors}"

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
    errors: list[BaseException] = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                notes.update_note(0, f"updated {i}", path=notes_path)
            except BaseException as exc:  # noqa: BLE001 - must be observed, not swallowed
                errors.append(exc)
                return
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        counts = [len(notes.read_notes(notes_path)) for _ in range(1000)]
    finally:
        stop.set()
        t.join(timeout=5)

    # A silently-dead writer thread would leave every read at a constant
    # 20 too — but over zero real concurrency, proving nothing.
    assert not errors, f"writer thread raised: {errors}"
    assert all(c == 20 for c in counts)


def test_atomic_replace_preserves_file_mode(notes_path, monkeypatch):
    # Guards _write_atomic's own chmod step (Proof 11), not the
    # pre/post-fix boundary: a literal revert of notes.py cannot show
    # this red, because pre-fix code rewrites through the SAME file
    # descriptor (open, truncate, write — never a new inode), so mode
    # survives by construction whether or not the write goes through a
    # new inode at all. Two-sided instead against a deliberately broken
    # _write_atomic that skips the chmod call onto the new inode —
    # exactly the behaviour this test exists to pin — which surfaces as
    # mkstemp's own fixed 0600 default.
    notes.append_note("first", path=notes_path)
    notes_path.chmod(0o644)

    monkeypatch.setattr(notes.os, "chmod", lambda *a, **k: None)
    notes.append_note("second, chmod disabled", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o600, (
        "the no-chmod shim should leave mkstemp's fixed default mode"
    )

    monkeypatch.undo()
    notes_path.chmod(0o644)
    notes.append_note("third, chmod restored", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o644


def test_atomic_replace_default_mode_for_a_new_file(notes_path, monkeypatch):
    # Same guard as above, for the brand-new-file path. A literal
    # pre-fix revert doesn't discriminate here either: old code creates
    # the file via a plain open(path, "a+"), which already gets the
    # umask-derived mode — identical to what _default_create_mode()
    # computes today. Two-sided against the same broken-chmod shim.
    monkeypatch.setattr(notes.os, "chmod", lambda *a, **k: None)
    notes.append_note("first, chmod disabled", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o600, (
        "the no-chmod shim should leave mkstemp's fixed default mode"
    )

    monkeypatch.undo()
    notes_path.unlink()
    notes.append_note("second, chmod restored", path=notes_path)
    expected = 0o666 & ~notes._current_umask()
    assert stat.S_IMODE(notes_path.stat().st_mode) == expected


def test_write_atomic_cleans_up_temp_file_on_failure(notes_path, monkeypatch):
    # The one branch of the new code this layer otherwise leaves
    # uncovered: if the replace itself fails, the temp file must not be
    # left behind in data/, and the original file must be untouched —
    # the "a reader never sees a partial file" contract would still be
    # breakable by a stray half-written temp file otherwise.
    notes.append_note("first", path=notes_path)
    original = notes_path.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(notes.os, "replace", boom)

    with pytest.raises(OSError):
        notes.append_note("second", path=notes_path)

    assert notes_path.read_text(encoding="utf-8") == original
    leftovers = [
        p for p in notes_path.parent.iterdir()
        if p.name.startswith(notes_path.name + ".") and p.name != notes_path.name + ".lock"
    ]
    assert leftovers == []


def test_default_notes_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "custom.md"))
    assert notes._default_notes_path() == tmp_path / "custom.md"
    monkeypatch.delenv("LOCAL_FITNESS_NOTES_PATH")
    monkeypatch.setenv("LOCAL_FITNESS_DATA_DIR", str(tmp_path))
    assert notes._default_notes_path() == tmp_path / "user_notes.md"
