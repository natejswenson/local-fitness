"""Tests for notes.py — the durable user-preference store."""
from __future__ import annotations

import stat
import threading
from datetime import datetime, timedelta

import pytest

from local_fitness import notes


@pytest.fixture
def notes_path(tmp_path):
    return tmp_path / "user_notes.md"


@pytest.fixture
def frozen_clock(monkeypatch):
    """Patches notes.datetime.now() to return a strictly increasing
    sequence of timestamps, one second apart, starting at a fixed
    instant. Every call anywhere inside notes.py -- append_note's stamp,
    update_note's stamp -- draws from this same sequence, so no two
    calls in a test using this fixture can ever land in the same
    wall-clock second. That removes the real flakiness a same-second
    tie can cause (an update racing another note's write, or a test
    straddling a second boundary) without touching any product code."""
    state = {"t": datetime(2026, 1, 1, 0, 0, 0)}

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = state["t"]
            state["t"] = current + timedelta(seconds=1)
            return current

    monkeypatch.setattr(notes, "datetime", _FrozenDatetime)
    return state


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


def test_update_note_enforces_the_cap(notes_path, frozen_clock):
    # Repro 3: only append_note called _rotate_to_fit, so replacing a
    # short note with a much longer one could leave the live file over
    # LIVE_FILE_MAX_BYTES until the next append -- measured at 4962 bytes
    # against a 4096 cap in the investigation. update_note must enforce
    # the cap itself, inside the same held lock as the rewrite that
    # caused it, after EVERY update -- not just eventually.
    #
    # Each note is updated immediately after it is appended (not: append
    # all six, then update all six). Appending all six up front and only
    # then updating them left the not-yet-updated originals sitting at
    # the OLDEST timestamps in the file for the whole loop -- once
    # rotation started firing, those still-untouched originals were
    # exactly what got evicted ahead of their own turn, so a later call
    # to update_note() on that now-archived handle correctly returned
    # None and `assert result is not None` failed on unmodified code.
    # That isn't a bug (a handle whose note rotated away is supposed to
    # refuse), it was a test-design flaw producing a wall-clock-shaped
    # failure (found in review). Interleaving means every handle this
    # test still holds has *just* been updated -- and update_note's own
    # protected_index always excludes the line it just rewrote from that
    # same call's rotation -- so no handle this loop still cares about is
    # ever a legitimate eviction target of a later iteration; only
    # already-completed, no-longer-tracked updates are.
    #
    # frozen_clock pins every timestamp to its own distinct, strictly
    # increasing second regardless, so ordering never depends on how
    # fast the real clock ticks during the run.
    seen_texts: list[str] = []
    for i in range(6):
        appended = notes.append_note(f"note {i}", path=notes_path)
        # Unique text per iteration (not a shared "x" * 800 for all six)
        # so the identity check below is real: six identical strings
        # would pass even if rotation archived one note twice and
        # dropped a different one -- exactly the wrong-note-hit failure
        # mode this issue exists to close (found in review).
        text = f"{i:02d}-" + "x" * 797
        result = notes.update_note(appended.handle, text, path=notes_path)
        assert result is not None
        updated, _ = result
        seen_texts.append(updated.text)
        live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
        assert len(live_bytes) <= notes.LIVE_FILE_MAX_BYTES

    archive = notes._archive_path(notes_path)
    assert archive.exists()  # rotation must actually have fired
    archived_texts = [n.text for n in notes.read_notes(archive)]
    live_texts = [n.text for n in notes.read_notes(notes_path)]
    # Every one of the six updated notes survives as its own distinct
    # note -- either still live or safely archived, never lost. Now a
    # real identity check: each of the six texts is unique, so this
    # multiset comparison would fail if rotation duplicated or dropped
    # one, not just if the count were wrong.
    assert sorted(archived_texts + live_texts) == sorted(seen_texts)


def test_update_note_never_evicts_its_own_just_refreshed_note(notes_path):
    # The cap check runs after update_note stamps its own fresh "now"
    # timestamp -- that must not make the note it just refreshed the
    # thing recent_first picks as oldest. Fill near the cap, then replace
    # the note that is genuinely oldest (both by position and by
    # timestamp) with a much longer one: it must survive, and whichever
    # note is now the true oldest by timestamp (note 1) is what's
    # archived instead.
    bullets = [
        f"- 2026-01-01T00:00:{i:02d} — preference number {i} with some padding text here"
        for i in range(48)
    ]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")
    oldest = notes.read_notes(notes_path)[0]

    result = notes.update_note(oldest.handle, "x" * 800, path=notes_path)
    assert result is not None
    updated_note, _ = result

    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) <= notes.LIVE_FILE_MAX_BYTES

    live_texts = [n.text for n in notes.read_notes(notes_path)]
    assert updated_note.text in live_texts

    archive = notes._archive_path(notes_path)
    assert archive.exists()
    archived_texts = [n.text for n in notes.read_notes(archive)]
    assert "preference number 1 with some padding text here" in archived_texts


def test_update_note_never_evicts_its_own_note_even_when_everyone_else_outranks_it(notes_path):
    # Round-1 review finding: the previous guard only worked because the
    # just-refreshed note's brand-new "now" timestamp usually IS the
    # newest in the file. It breaks the moment something else in the
    # file outranks that fresh timestamp -- a hand-edited bullet with a
    # FUTURE date is the deterministic way to construct that without
    # depending on real wall-clock ties between two calls. Every other
    # live bullet here is dated in 2099, so after the rewrite the
    # just-updated line (timestamp = real "now") is the single oldest
    # thing in the file by `_recency_key` -- exactly what used to make
    # `_rotate_to_fit` archive it, silently orphaning the handle
    # `update_user_note` had just told the caller was live.
    fillers = [
        f"- 2099-01-01T00:00:{i:02d} — future filler note number {i:02d} "
        "with some padding text here to take up room"
        for i in range(45)
    ]
    target_line = "- 2020-01-01T00:00:00 — old target note to be replaced"
    notes_path.write_text("\n".join([target_line, *fillers]) + "\n", encoding="utf-8")
    target = notes.read_notes(notes_path)[0]

    result = notes.update_note(target.handle, "x" * 800, path=notes_path)
    assert result is not None
    updated_note, _ = result

    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) <= notes.LIVE_FILE_MAX_BYTES

    # The just-updated note survives live, under its NEW handle...
    live_texts = [n.text for n in notes.read_notes(notes_path)]
    assert updated_note.text in live_texts
    # ...and that handle keeps resolving on the very next call, which is
    # exactly what a caller checks to confirm the write actually landed.
    assert notes.update_note(updated_note.handle, "y", path=notes_path) is not None

    # Rotation still fired, and it took one of the future-dated fillers
    # instead -- eviction happened, it just didn't pick the wrong note.
    archive = notes._archive_path(notes_path)
    assert archive.exists()
    archived_texts = {n.text for n in notes.read_notes(archive)}
    assert any(t.startswith("future filler note number") for t in archived_texts)


def test_update_note_protected_index_tracks_the_target_across_multiple_evictions(notes_path):
    # The two tests above both protect a target sitting at file position
    # 0 -- _rotate_to_fit never pops anything *before* it, so the
    # `protected_index -= 1` shift-adjustment in _rotate_to_fit is never
    # exercised: with that single line deleted (no other change),
    # every test in this file still passes (confirmed directly:
    # 3/3 green with the line removed, per review). This test puts the
    # target at position 5, behind five bullets that are evicted first,
    # so protecting it correctly *requires* protected_index to track the
    # target's shifting position across five separate eviction rounds,
    # not just guard a fixed index.
    #
    # Five ancient bullets (year 2000) are older than everything else,
    # so they are evicted first regardless of protection -- that part of
    # the ordering doesn't depend on the fix under test. The target
    # starts at position 5, older than the ancients' *contents* don't
    # matter, only that they sort before it. Forty-five future-dated
    # (2099) fillers follow: once the five ancients are gone, the
    # just-updated target (stamped with the real "now", which is older
    # than 2099) becomes the true oldest of everything left -- so if
    # protected_index has gone stale (not decremented across those five
    # prior evictions), it now points at whatever shifted into the
    # target's old slot, the target is left unprotected, and -- being
    # the genuine oldest survivor -- it is exactly what gets evicted
    # next, silently orphaning the handle update_note just returned.
    ancients = [
        f"- 2000-01-01T00:00:{i:02d} — ancient filler {i:02d} with some padding text here"
        for i in range(5)
    ]
    target_line = "- 2020-01-01T00:00:00 — old target note to be replaced"
    fillers = [
        f"- 2099-01-01T00:00:{i:02d} — future filler note number {i:02d} "
        "with some padding text here to take up room"
        for i in range(45)
    ]
    notes_path.write_text(
        "\n".join([*ancients, target_line, *fillers]) + "\n", encoding="utf-8",
    )
    target = notes.read_notes(notes_path)[5]
    assert target.text == "old target note to be replaced"

    result = notes.update_note(target.handle, "x" * 800, path=notes_path)
    assert result is not None
    updated_note, _ = result

    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) <= notes.LIVE_FILE_MAX_BYTES

    # The just-updated note survives live -- this is the assertion that
    # fails when protected_index is not adjusted for the five prior
    # evictions in front of it.
    live_texts = [n.text for n in notes.read_notes(notes_path)]
    assert updated_note.text in live_texts
    assert notes.update_note(updated_note.handle, "y", path=notes_path) is not None

    # Rotation fired well past the five ancients -- some future filler
    # was evicted too, confirming the cap check kept going after the
    # ancients ran out rather than stopping short.
    archive = notes._archive_path(notes_path)
    archived_texts = {n.text for n in notes.read_notes(archive)}
    assert any(t.startswith("ancient filler") for t in archived_texts)
    assert any(t.startswith("future filler note number") for t in archived_texts)


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


def test_recent_first_ranks_a_refreshed_note_first(notes_path):
    # Repro 6: update_note refreshes a note's timestamp in place WITHOUT
    # moving its line. A plain reversed(file order) then leaves the
    # older, unrefreshed note first; recent_first must rank by the
    # timestamp itself, not by position.
    notes_path.write_text(
        "- 2026-01-01T08:00:00 — OLD note, superseded.\n"
        "- 2026-02-01T08:00:00 — NEWER conflicting note.\n",
        encoding="utf-8",
    )
    old = next(n for n in notes.read_notes(notes_path) if "OLD" in n.text)
    result = notes.update_note(
        old.handle, "OLD note, but just refreshed today.", path=notes_path
    )
    assert result is not None

    ranked = notes.recent_first(notes.read_notes(notes_path))
    assert [n.text for n in ranked] == [
        "OLD note, but just refreshed today.",
        "NEWER conflicting note.",
    ]

    rendered = notes.render_for_prompt(notes_path)
    assert rendered.splitlines()[0].endswith("OLD note, but just refreshed today.")


def test_rotation_evicts_oldest_by_timestamp_not_position(notes_path):
    # Repro 7: a note refreshed today but still sitting at file position 0
    # (the oldest POSITION) must survive rotation, and whichever note is
    # actually oldest BY TIMESTAMP is what gets archived instead. On dev,
    # position-based eviction (lines.pop(0)) archived the just-refreshed
    # note first.
    bullets = [
        f"- 2026-01-01T00:00:{i:02d} — preference number {i} with some padding text here"
        for i in range(60)
    ]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")

    refreshed_handle = notes.read_notes(notes_path)[0].handle
    result = notes.update_note(
        refreshed_handle,
        "JUST REFRESHED TODAY, lead with the workout card",
        path=notes_path,
    )
    assert result is not None
    refreshed_note, _ = result

    notes.append_note("one more ordinary preference with padding text", path=notes_path)

    live_texts = [n.text for n in notes.read_notes(notes_path)]
    assert refreshed_note.text in live_texts

    archive = notes._archive_path(notes_path)
    assert archive.exists()  # rotation must actually have fired
    archived_texts = [
        parsed.text
        for parsed in (notes._parse_line(ln) for ln in archive.read_text(encoding="utf-8").splitlines())
        if parsed is not None
    ]
    assert refreshed_note.text not in archived_texts
    # Note 0 was refreshed to today, so note 1 (2026-01-01T00:00:01) is
    # now the oldest live timestamp and must be what's evicted first.
    assert "preference number 1 with some padding text here" in archived_texts


def test_recent_first_sorts_undated_and_malformed_timestamps_last(notes_path):
    # A naive (timestamp, position) key sorts an empty/malformed string
    # FIRST (it compares less than any real ISO timestamp) — which would
    # promote a hand-edited undated bullet to the very front of the
    # prompt. recent_first must fall back to putting it last instead.
    notes_path.write_text(
        "- not-a-valid-timestamp — malformed date\n"
        "- just text no separator\n"
        "- 2026-01-01T00:00:00 — dated note\n",
        encoding="utf-8",
    )
    ranked = notes.recent_first(notes.read_notes(notes_path))
    assert ranked[0].text == "dated note"
    assert {ranked[1].text, ranked[2].text} == {"malformed date", "just text no separator"}
    # And it must not raise — a hand-edited file is exactly the case this
    # module is designed to tolerate.


def test_rotation_terminates_on_pure_prose_over_cap(notes_path):
    # A file with no evictable bullet at all — pure hand-edited prose,
    # over the cap on its own — must not spin forever looking for a
    # bullet to evict. The loop terminates and the write proceeds over
    # budget rather than discarding content that isn't a note.
    prose = "# just a heading, no bullets at all\n" * 200
    assert len(prose.encode("utf-8")) > notes.LIVE_FILE_MAX_BYTES
    notes_path.write_text(prose, encoding="utf-8")

    new_note = notes.append_note("a real note finally", path=notes_path)
    assert new_note.text == "a real note finally"

    live_text = notes_path.read_text(encoding="utf-8")
    assert "# just a heading" in live_text
    assert "a real note finally" in live_text
    archive = notes._archive_path(notes_path)
    assert not archive.exists()  # nothing was ever evictable, so nothing rotated


def test_default_notes_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "custom.md"))
    assert notes._default_notes_path() == tmp_path / "custom.md"
    monkeypatch.delenv("LOCAL_FITNESS_NOTES_PATH")
    monkeypatch.setenv("LOCAL_FITNESS_DATA_DIR", str(tmp_path))
    assert notes._default_notes_path() == tmp_path / "user_notes.md"
