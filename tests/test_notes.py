"""Tests for notes.py — the durable user-preference store."""
from __future__ import annotations

import stat
import threading
from unittest import mock

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


def test_locked_rewrite_no_lost_write_across_concurrent_append(notes_path, monkeypatch):
    # Proof 9 (positive half). Per the design, this guards the refactor
    # rather than reproducing a live failure: a literal revert of
    # notes.py cannot show this red, because pre-fix update_note already
    # holds one flock across its own read+write, so it is ALSO safe from
    # this exact race — the design says as much ("passes on dev today").
    # The real regression this guards is a future rewrite of update_note
    # or delete_note that splits reading from writing without going
    # through _locked_rewrite; its two-sidedness is against exactly such
    # a rewrite (see the mutant check below), not a pre/post-fix
    # comparison.
    #
    # Round-3 review fix: the previous shape started the concurrent
    # append from an `_after_read_hook`, which `_locked_rewrite` only
    # invokes AFTER it has already taken the real lock and done its own
    # internal read. That is too late to guard the class of regression
    # design.md §0 names — a future `update_note` that reads and
    # resolves OUTSIDE the lock, and only assigns `ctx.text` inside it —
    # because such a writer's stale snapshot is captured before it ever
    # calls `_locked_rewrite` at all, i.e. strictly before the hook could
    # ever fire. No hook fired from inside `_locked_rewrite` can land
    # earlier than a read that happens before `_locked_rewrite` is even
    # called. So the injection point moves to the only place that
    # genuinely precedes the writer's own lock acquisition: the top of
    # the `wrapped` shim itself, before it delegates to the real
    # implementation at all — "after the writer's own read, before it
    # takes the lock," per the review — and the concurrent append is
    # joined to completion right there, deterministically, before
    # delegating. (An earlier draft of this fix tried to prove genuine
    # OS-level lock *contention* here too, via an Event or a
    # threading.Barrier; both made the outcome a race between the two
    # threads for who reaches flock() first, and empirically that race
    # is won by whichever ordering happens to leave the write intact —
    # so it stopped catching the regression it was built to catch. Join
    # removes the race: the append has always fully landed, or raised,
    # by the time update_note's own call is even attempted. Proving the
    # lock ITSELF is what serializes genuinely-concurrent writers is a
    # different property and is what the no-op-lock companion test
    # below exists for — this test is not a claim about contention.)
    #
    # The held side is a PRODUCTION writer (update_note), not a
    # hand-rolled _locked_rewrite block — a hand-rolled block can go
    # green forever even if update_note itself later stops using the
    # lock. The seam: update_note calls the module-global
    # notes._locked_rewrite, so wrapping that global injects here into
    # the one call this test needs to observe, and delegates every other
    # call (including the concurrent append's own) straight through
    # unchanged.
    notes.append_note("first", path=notes_path)
    thread_box: dict[str, threading.Thread] = {}
    errors: list[BaseException] = []
    real_locked_rewrite = notes._locked_rewrite
    hooked = {"fired": False}

    def append_second():
        try:
            notes.append_note("second, concurrent", path=notes_path)
        except BaseException as exc:  # noqa: BLE001 - must be observed, not swallowed
            errors.append(exc)

    def wrapped(path, *, _after_read_hook=None):
        if hooked["fired"]:
            return real_locked_rewrite(path, _after_read_hook=_after_read_hook)
        hooked["fired"] = True

        # Everything below runs, and completes, BEFORE update_note's own
        # call is delegated to the real implementation — i.e. before
        # update_note (correct or, hypothetically, a regressed
        # reads-outside-the-lock rewrite of it) has taken its lock. This
        # is the window a regression's stale pre-lock snapshot would
        # already have closed over, so landing the concurrent write here
        # is the only placement that can catch it.
        t = threading.Thread(target=append_second)
        t.start()
        thread_box["thread"] = t
        t.join(timeout=5)
        assert not t.is_alive(), "the concurrent append never finished"

        return real_locked_rewrite(path, _after_read_hook=_after_read_hook)

    monkeypatch.setattr(notes, "_locked_rewrite", wrapped)

    notes.update_note(0, "first, updated", path=notes_path)

    assert not errors, f"the concurrent append raised: {errors}"

    survivors = notes.read_notes(notes_path)
    assert len(survivors) == 2, "expected exactly two notes, not an extra or duplicated bullet"
    assert {n.text for n in survivors} == {"first, updated", "second, concurrent"}


def test_locked_rewrite_catches_a_reads_outside_the_lock_regression(notes_path, monkeypatch):
    # This is the round-3 review's own finding, made permanent rather than
    # only checked by hand: it demonstrated that the OLD version of the
    # test above (concurrent write injected from _after_read_hook, i.e.
    # after the lock was already taken) still reported "no lost write"
    # against a rewrite of update_note that reads and resolves OUTSIDE
    # the lock and only assigns ctx.text inside it — precisely the
    # regression class design.md §0 exists to guard. Reusing the SAME
    # harness as the test above (concurrent append joined to completion
    # before the writer under test takes its lock), but pointed at that
    # mutant instead of the real notes.update_note, must show the write
    # LOST — proving the harness actually discriminates the regression
    # it exists to catch, not just that the real code happens to pass it.
    def mutant_update_note(line_index, new_text, path=None):
        p = path or notes._default_notes_path()
        if not p.exists():
            return None
        # OUTSIDE THE LOCK: read and resolve against a snapshot — the
        # exact shape the review's finding describes.
        existing_text = p.read_text(encoding="utf-8")
        lines = existing_text.splitlines(keepends=True)
        if line_index < 0 or line_index >= len(lines):
            return None
        if notes._parse_line(lines[line_index]) is None:
            return None
        had_newline = lines[line_index].endswith("\n")
        lines[line_index] = f"- 2026-01-01T00:00:00 — {new_text}" + (
            "\n" if had_newline else ""
        )
        stale_full_text = "".join(lines)
        # INSIDE THE LOCK: only ctx.text is assigned; nothing here
        # re-reads or re-resolves against the fresh, locked state.
        with notes._locked_rewrite(p) as ctx:
            ctx.text = stale_full_text
        return None

    notes.append_note("first", path=notes_path)
    thread_box: dict[str, threading.Thread] = {}
    errors: list[BaseException] = []
    landed = threading.Event()
    real_locked_rewrite = notes._locked_rewrite
    hooked = {"fired": False}

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
        t = threading.Thread(target=append_second)
        t.start()
        thread_box["thread"] = t
        t.join(timeout=5)
        assert not t.is_alive(), "the concurrent append never finished"
        return real_locked_rewrite(path, _after_read_hook=_after_read_hook)

    monkeypatch.setattr(notes, "_locked_rewrite", wrapped)

    mutant_update_note(0, "first, updated", path=notes_path)

    # Round-4 review fix: without this, neutering the injection so the
    # concurrent append never starts at all would leave "second,
    # concurrent" absent for the trivial reason that it was never
    # written — indistinguishable, by the assertion below alone, from
    # the harness genuinely catching the mutant. Its sibling test
    # (the no-op companion) already guards exactly this with its own
    # `landed.wait(...)`; this test needs the same guard.
    assert landed.wait(timeout=5), "the concurrent append never completed"
    assert not errors, f"the concurrent append raised: {errors}"

    remaining = {n.text for n in notes.read_notes(notes_path)}
    assert "second, concurrent" not in remaining, (
        "the harness failed to catch a writer that reads and resolves "
        "outside the lock — it silently overwrote the concurrent append"
    )


def test_locked_rewrite_serializes_a_concurrent_appender(notes_path, monkeypatch):
    # Round-4: restores the positive lock-serialization coverage round 3
    # dropped when it moved test_locked_rewrite_no_lost_write_across_
    # concurrent_append's injection to the top of the wrapped shim (a
    # join *before* the held writer ever takes its lock — see that
    # test's comment). That move made the two writers strictly
    # sequential: the concurrent append always finishes before
    # update_note's own _locked_rewrite call is even entered, so nothing
    # in that test exercises the lock's own blocking behaviour, and
    # round 2's mutation coverage silently evaporated — deleting BOTH
    # `fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)` calls from
    # src/local_fitness/notes.py left the whole file green 10/10 (see
    # test.md § Two-sided for the confirmed re-run).
    #
    # This test is deliberately injected at the SAME point as its no-op
    # companion below (`_after_read_hook` — fired by `_locked_rewrite`
    # itself, after the held writer already holds the real lock and has
    # done its own read), so the two form a genuine lock-enabled /
    # lock-disabled pair over the identical interleaving, per design.md
    # Proof 9. It proves the lock is load-bearing rather than
    # incidental ordering by checking not just that the concurrent
    # writer's own `flock()` call was *reached* (round 2's version did
    # only that — flagged as weaker than claimed, since setting the
    # signal before calling the real `flock` proves an attempt, not a
    # block) but that it has not yet been *granted* while the held
    # writer still holds the lock — something only true if the two
    # really are contending for the same lock. No sleep, no timing
    # guess: the held writer cannot have released before this check
    # runs (it hasn't even reached the with-block's own body yet), so
    # if the concurrent writer's flock() had already returned, the two
    # calls could not have been serialised by the same lock.
    #
    # The held writer is the PRODUCTION update_note (round 1's finding:
    # a hand-rolled _locked_rewrite block can't observe a regression in
    # the real writers), reached via the same monkeypatched-
    # notes._locked_rewrite seam every other test in this file uses.
    notes.append_note("first", path=notes_path)
    thread_box: dict[str, threading.Thread] = {}
    errors: list[BaseException] = []
    real_locked_rewrite = notes._locked_rewrite
    real_flock = notes.fcntl.flock
    reached = threading.Event()
    acquired = threading.Event()
    hooked = {"fired": False}

    def flock_wrapper(*a, **k):
        reached.set()
        result = real_flock(*a, **k)
        acquired.set()
        return result

    def append_second():
        try:
            notes.append_note("second, concurrent", path=notes_path)
        except BaseException as exc:  # noqa: BLE001 - must be observed, not swallowed
            errors.append(exc)

    def hook():
        monkeypatch.setattr(notes.fcntl, "flock", flock_wrapper)
        t = threading.Thread(target=append_second)
        t.start()
        thread_box["thread"] = t
        assert reached.wait(timeout=5), (
            "the concurrent append never reached its own flock() call "
            "-- deleting the real fcntl.flock() calls removes the only "
            "call site this wrapper is ever installed onto"
        )
        # The held writer's own lock is still ours here -- we are inside
        # _locked_rewrite's _after_read_hook, called before the with-block
        # body (update_note's own logic) has even run, let alone before
        # the lock is released on exit. So the concurrent writer's own
        # flock() call cannot yet have been granted if it is genuinely
        # contending for the same lock.
        assert not acquired.is_set(), (
            "the concurrent writer's flock() call returned before the "
            "held writer released its lock -- the two are not "
            "contending for the same lock"
        )

    def wrapped(path, *, _after_read_hook=None):
        if hooked["fired"]:
            return real_locked_rewrite(path, _after_read_hook=_after_read_hook)
        hooked["fired"] = True
        return real_locked_rewrite(path, _after_read_hook=hook)

    monkeypatch.setattr(notes, "_locked_rewrite", wrapped)

    notes.update_note(0, "first, updated", path=notes_path)

    thread_box["thread"].join(timeout=5)
    assert not thread_box["thread"].is_alive()
    assert not errors, f"the concurrent append raised: {errors}"

    survivors = notes.read_notes(notes_path)
    assert len(survivors) == 2, "expected exactly two notes, not an extra or duplicated bullet"
    assert {n.text for n in survivors} == {"first, updated", "second, concurrent"}


def test_locked_rewrite_loses_a_write_when_the_lock_is_a_no_op(notes_path, monkeypatch):
    # Two-sided pair for test_locked_rewrite_serializes_a_concurrent_appender
    # above (same _after_read_hook injection point, per design.md Proof 9):
    # stub the lock acquisition to a no-op and force the identical
    # interleaving — the second writer now runs to completion (nothing
    # blocks it) before the held write lands, and gets clobbered. Proves
    # the lock above is load-bearing, not incidental timing. Same
    # production-writer seam as above: update_note is the held side,
    # append_note is the concurrent side.
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


def test_atomic_replace_preserves_file_mode(notes_path):
    # Guards _write_atomic's own chmod step (Proof 11). Round-4 review
    # fix: this comment used to claim a literal revert of notes.py
    # "cannot show this red" — that claim is false. Re-run against a
    # literal da31349 revert, this test's own first assertion
    # (`== 0o600`) fails (`assert 420 == 384`): pre-fix code never
    # routes through mkstemp/_write_atomic at all, so the chmod-disable
    # shim below has no effect and the file keeps its already-chmod'd
    # 0o644 mode instead of mkstemp's 0o600 default. So a literal
    # revert *does* discriminate here — just not for the reason "mode
    # survives by construction" implies. This test is still built
    # against the in-test chmod-disabled shim rather than a revert
    # because it isolates the one behaviour this test exists to pin —
    # _write_atomic's own chmod call — instead of relying on the
    # coincidence that pre-fix code also fails, for an unrelated
    # reason, to reach this code path at all. Two-sided against a
    # deliberately broken _write_atomic that skips the chmod call onto
    # the new inode, which surfaces as mkstemp's own fixed 0600 default.
    #
    # The shim is scoped with mock.patch.object as a context manager,
    # not monkeypatch.setattr()+monkeypatch.undo(): pytest hands every
    # autouse fixture in this test the SAME function-scoped monkeypatch
    # instance, so an undo() here would also strip tests/conftest.py's
    # hard suite guards (_no_live_sdk_calls, _no_live_garmin_calls,
    # _no_live_smtp_calls, _no_ambient_calendar_credentials,
    # _no_live_calendar_calls) for the remainder of the test body.
    notes.append_note("first", path=notes_path)
    notes_path.chmod(0o644)

    with mock.patch.object(notes.os, "chmod", lambda *a, **k: None):
        notes.append_note("second, chmod disabled", path=notes_path)
        assert stat.S_IMODE(notes_path.stat().st_mode) == 0o600, (
            "the no-chmod shim should leave mkstemp's fixed default mode"
        )

    notes_path.chmod(0o644)
    notes.append_note("third, chmod restored", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o644


def test_atomic_replace_default_mode_for_a_new_file(notes_path):
    # Same guard as above, for the brand-new-file path, and the same
    # round-4 correction: re-run against a literal da31349 revert, this
    # test's own first assertion (`== 0o600`) also fails
    # (`assert 420 == 384`) for the identical reason — pre-fix code
    # never reaches mkstemp/_write_atomic at all, so this test does not
    # rely on old code being coincidentally safe from this shim; it is
    # built against the shim, not a revert, because the shim isolates
    # exactly this path's own chmod call rather than the accident of
    # pre-fix code not taking it. Two-sided against the same
    # broken-chmod shim, scoped the same mock.patch.object way (see the
    # test above).
    with mock.patch.object(notes.os, "chmod", lambda *a, **k: None):
        notes.append_note("first, chmod disabled", path=notes_path)
        assert stat.S_IMODE(notes_path.stat().st_mode) == 0o600, (
            "the no-chmod shim should leave mkstemp's fixed default mode"
        )
    notes_path.unlink()

    # The green half is pinned against a literal expected mode with the
    # umask input fixed, not 0o666 & ~notes._current_umask() — reusing
    # that exact formula as "expected" would assert
    # _default_create_mode() against itself, so a wrong formula (or a
    # broken _current_umask()) could never fail this. Fixing the umask
    # input makes the expected value independent of both, and of the
    # host's actual umask.
    with mock.patch.object(notes, "_current_umask", lambda: 0o022):
        notes.append_note("second, chmod restored", path=notes_path)
    assert stat.S_IMODE(notes_path.stat().st_mode) == 0o644


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
