"""Tests for notes.py — the durable user-preference store."""
from __future__ import annotations

import logging
import stat
import threading
from datetime import datetime, timedelta
from unittest import mock

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


@pytest.fixture
def pinned_clock(monkeypatch):
    """Patches notes.datetime.now() to return one fixed instant on every
    call -- the same-wall-clock-second case frozen_clock deliberately
    removes, and the only condition under which a writer can mint two
    identical (timestamp, text) pairs on its own. Returns the instant, so
    a test can plant a bullet already carrying it. Adversarial by design:
    the collision guard is unreachable without a clock that repeats."""
    instant = datetime(2026, 1, 1, 0, 0, 0)

    class _PinnedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(notes, "datetime", _PinnedDatetime)
    return instant


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


def test_update_never_manufactures_a_handle_collision(notes_path, pinned_clock):
    # Two updates rewriting two different notes to the same text inside
    # one wall-clock second. append_note has always re-stamped out of
    # this; update_note used to stamp with no handle check at all and
    # left two live bullets sharing one address.
    notes.append_note("first preference", path=notes_path)
    notes.append_note("second preference", path=notes_path)
    live = notes.read_notes(notes_path)

    first = notes.update_note(live[0].handle, "the same new text", path=notes_path)
    second = notes.update_note(live[1].handle, "the same new text", path=notes_path)
    assert first is not None and second is not None
    assert first[0].handle != second[0].handle

    handles = [n.handle for n in notes.read_notes(notes_path)]
    assert len(handles) == 2
    assert len(set(handles)) == 2


def test_update_restamps_when_its_stamp_would_hit_an_existing_bullet(notes_path, pinned_clock):
    # The other shape: the update's own (now, new text) pair is already
    # on disk, so the naive stamp lands on a live bullet's handle rather
    # than on another update's.
    pinned = pinned_clock.isoformat()
    notes_path.write_text(
        f"- {pinned} — the text both bullets want\n"
        "- 2020-01-01T00:00:00 — the note being rewritten\n",
        encoding="utf-8",
    )
    target = next(n for n in notes.read_notes(notes_path)
                  if n.text == "the note being rewritten")

    result = notes.update_note(target.handle, "the text both bullets want", path=notes_path)
    assert result is not None
    updated, _duplicates = result
    assert updated.timestamp != pinned  # re-stamped forward instead of colliding

    handles = [n.handle for n in notes.read_notes(notes_path)]
    assert len(handles) == 2
    assert len(set(handles)) == 2


def test_update_leaves_same_text_notes_independently_addressable(notes_path, pinned_clock):
    # Two bullets carrying identical *text* stays legal — the guard
    # re-stamps rather than refusing, so "make these two say the same
    # thing" is not an error. What must stop is one handle addressing
    # two bullets: a delete used to leave a live twin behind.
    notes.append_note("first preference", path=notes_path)
    notes.append_note("second preference", path=notes_path)
    live = notes.read_notes(notes_path)
    first = notes.update_note(live[0].handle, "the same new text", path=notes_path)
    second = notes.update_note(live[1].handle, "the same new text", path=notes_path)
    assert first is not None and second is not None

    assert notes.delete_note(first[0].handle, path=notes_path) == 1
    remaining = notes.read_notes(notes_path)
    assert [n.text for n in remaining] == ["the same new text"]
    assert remaining[0].handle == second[0].handle

    survivor = notes.update_note(second[0].handle, "now distinct", path=notes_path)
    assert survivor is not None
    assert survivor[1] == 1


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


def _break_the_archive(notes_path):
    """Make the archive path unwritable the way an operator's environment
    can: a directory sits where the file belongs, so the append inside
    ``_append_archive`` raises ``IsADirectoryError`` (an ``OSError``).
    Returns the archive path. The lock sidecar is a separate file, so the
    failure lands on the content write, not on the lock."""
    archive = notes._archive_path(notes_path)
    archive.mkdir()
    return archive


def test_append_keeps_evicted_bullets_live_when_the_archive_cannot_be_written(
    notes_path, caplog,
):
    # Issue #232 item 2. _append_archive logged the OSError and returned
    # the same None it returns on success, so append_note truncated the
    # live file regardless and the evicted preferences existed nowhere:
    # 60 -> 57 live bullets, archive empty, 3 preferences destroyed. The
    # 4 KB cap bounds prompt size; it does not license deletion.
    original_texts = [
        f"note number {i:03d} with some padding text to bulk it up" for i in range(60)
    ]
    bullets = [f"- 2026-01-01T00:00:{i:02d} — {t}" for i, t in enumerate(original_texts)]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")
    archive = _break_the_archive(notes_path)

    with caplog.at_level(logging.WARNING, logger=notes.__name__):
        new_note = notes.append_note("Never comment on my weekend sleep.", path=notes_path)

    live_texts = [n.text for n in notes.read_notes(notes_path)]
    for text in original_texts:
        assert text in live_texts  # nothing evicted, because nothing could be archived
    assert new_note.text in live_texts  # and the incoming preference is still saved
    assert len(live_texts) == len(original_texts) + 1

    # Knowingly over budget — the named, accepted consequence of not
    # evicting. A fat prompt beats a destroyed preference.
    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) > notes.LIVE_FILE_MAX_BYTES

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(archive) in m for m in warnings)
    assert any("over budget" in m for m in warnings)


def test_update_keeps_evicted_bullets_live_when_the_archive_cannot_be_written(
    notes_path, caplog,
):
    # Same defect on the path PR #234 opened, where it costs more: one
    # capacity-tripping update rotates as many bullets as the new text
    # costs, so a broken archive destroyed six preferences at a time.
    original_texts = [
        f"note number {i:03d} with some padding text to bulk it up" for i in range(50)
    ]
    bullets = [f"- 2026-01-01T00:00:{i:02d} — {t}" for i, t in enumerate(original_texts)]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")
    assert len("\n".join(bullets).encode("utf-8")) < notes.LIVE_FILE_MAX_BYTES
    archive = _break_the_archive(notes_path)

    target = notes.read_notes(notes_path)[10]
    grown = "grow this one preference until it trips the cap " * 15

    with caplog.at_level(logging.WARNING, logger=notes.__name__):
        result = notes.update_note(target.handle, grown, path=notes_path)

    assert result is not None
    new_note, _duplicates = result
    live = notes.read_notes(notes_path)
    live_texts = [n.text for n in live]
    for text in original_texts:
        if text == target.text:
            continue  # this one was rewritten, on purpose
        assert text in live_texts
    assert new_note.text in live_texts
    assert len(live_texts) == len(original_texts)

    live_bytes = notes_path.read_text(encoding="utf-8").encode("utf-8")
    assert len(live_bytes) > notes.LIVE_FILE_MAX_BYTES

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(archive) in m for m in warnings)
    assert any("over budget" in m for m in warnings)


def test_update_position_still_points_at_the_updated_bullet_when_archiving_fails(
    notes_path,
):
    # Guards the `new_position = protected_after` assignment moving inside
    # the archive-succeeded branch. On the skip path nothing was popped,
    # so the position _rotate_to_fit computed is short by however many
    # lines it WOULD have evicted, and the returned Note points at some
    # other user's preference. Position is a hint, but a hint that points
    # at the wrong bullet is worse than no hint.
    original_texts = [
        f"note number {i:03d} with some padding text to bulk it up" for i in range(50)
    ]
    bullets = [f"- 2026-01-01T00:00:{i:02d} — {t}" for i, t in enumerate(original_texts)]
    notes_path.write_text("\n".join(bullets) + "\n", encoding="utf-8")
    _break_the_archive(notes_path)

    target = notes.read_notes(notes_path)[10]
    grown = "grow this one preference until it trips the cap " * 15
    result = notes.update_note(target.handle, grown, path=notes_path)

    assert result is not None
    new_note, _duplicates = result
    live = notes.read_notes(notes_path)
    assert new_note.position == 10
    assert live[new_note.position].handle == new_note.handle
    assert live[new_note.position].text == new_note.text




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
    first_handle = notes.append_note("first", path=notes_path).handle
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

    notes.update_note(first_handle, "first, updated", path=notes_path)

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
    def mutant_update_note(handle, new_text, path=None):
        p = path or notes._default_notes_path()
        if not p.exists():
            return None
        # OUTSIDE THE LOCK: read and resolve against a snapshot — the
        # exact shape the review's finding describes.
        existing_text = p.read_text(encoding="utf-8")
        lines = existing_text.splitlines(keepends=True)
        match_index = None
        for i, ln in enumerate(lines):
            parsed = notes._parse_line(ln)
            if parsed is not None and parsed.handle == handle:
                match_index = i
                break
        if match_index is None:
            return None
        had_newline = lines[match_index].endswith("\n")
        lines[match_index] = f"- 2026-01-01T00:00:00 — {new_text}" + (
            "\n" if had_newline else ""
        )
        stale_full_text = "".join(lines)
        # INSIDE THE LOCK: only ctx.text is assigned; nothing here
        # re-reads or re-resolves against the fresh, locked state.
        with notes._locked_rewrite(p) as ctx:
            ctx.text = stale_full_text
        return None

    first_handle = notes.append_note("first", path=notes_path).handle
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

    mutant_update_note(first_handle, "first, updated", path=notes_path)

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
    first_handle = notes.append_note("first", path=notes_path).handle
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

    notes.update_note(first_handle, "first, updated", path=notes_path)

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
    first_handle = notes.append_note("first", path=notes_path).handle
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

    notes.update_note(first_handle, "first, updated", path=notes_path)

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
        current = notes.read_notes(notes_path)[0].handle
        while not stop.is_set():
            try:
                result = notes.update_note(current, f"updated {i}", path=notes_path)
            except BaseException as exc:  # noqa: BLE001 - must be observed, not swallowed
                errors.append(exc)
                return
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
