"""User-notes store — durable preferences the chat agent learns over time.

Notes are bullets in a single markdown file, one per line, written by the
``save_user_note`` MCP tool when the agent recognises a durable user
preference ("I wish you were kinder", "lead with the workout card",
etc). The file's contents are injected into ``system_prompt`` so every
brief and chat reads them.

Format on disk (``data/user_notes.md``)::

    - 2026-04-28T11:32:14 — Roast me when I'm slipping; encouragement softens motivation.
    - 2026-04-26T08:30:01 — Marathon training starts in May; CTL trajectory matters more than the absolute number.

Hand-editable; rewrite or delete lines directly with any text editor and
the next prompt build picks up the change. Every write reads the file,
decides the new content, and rewrites it inside one held critical
section: an exclusive lock on a sidecar ``<name>.lock`` file — never on
the note file itself, since that file is about to be replaced out from
under any lock held on its own descriptor — serialises writers, and the
new content lands via a same-directory temp file that is
``os.replace``'d over the original. A reader, including a text editor
with the file open, therefore never observes a partial file: only the
whole old one or the whole new one — the zero-byte window a plain
truncate-then-write leaves is closed by construction. A 4 KB live cap
keeps prompt context bounded — older bullets overflow to
``user_notes.archive.md`` rather than getting lost.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

LOG = logging.getLogger(__name__)

# 4 KB live-file budget — keeps the system-prompt injection bounded.
# Tested: ~40-50 typical preference bullets fit, plenty for one user.
LIVE_FILE_MAX_BYTES = 4096


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_notes_path() -> Path:
    """Resolve the notes file path. Honors LOCAL_FITNESS_NOTES_PATH; falls
    back to the same data dir convention as the SQLite DB."""
    override = os.environ.get("LOCAL_FITNESS_NOTES_PATH")
    if override:
        return Path(override)
    data_override = os.environ.get("LOCAL_FITNESS_DATA_DIR")
    base = Path(data_override) if data_override else _PROJECT_ROOT / "data"
    return base / "user_notes.md"


def _archive_path(live_path: Path) -> Path:
    return live_path.with_name(live_path.stem + ".archive" + live_path.suffix)


@dataclass(frozen=True)
class Note:
    handle: str  # 8 lowercase hex chars of sha256(timestamp + "\n" + text) —
                 # the address a caller uses to target this note. Derived on
                 # every read, never stored; survives an unrelated delete or
                 # rotation because it carries no position. See _handle().
    position: int  # 0-indexed offset in the live file AT READ TIME. Not an
                    # identity — a write elsewhere renumbers it — so this is
                    # a hint for the writer only and never reaches a tool
                    # payload. (Was `line`; renamed so the distinction from
                    # `handle` is visible at every call site.)
    timestamp: str  # ISO-8601 second-precision
    text: str


def _handle(timestamp: str, text: str) -> str:
    """The content address for a note: 8 lowercase hex characters of
    sha256(timestamp + "\\n" + text).

    Position-independent by construction — the only thing that changes it
    is the note's own timestamp or text changing, which is exactly the
    case (an update, or a hand-edit) where re-addressing is correct
    behaviour rather than a bug. Two live notes collide only if they are
    byte-for-byte identical in both fields; see _RewriteContext.resolve
    for how a collision is tolerated rather than treated as ambiguous.
    Short, not cryptographically strong: the only adversary here is a
    stale value, and the model has to transcribe it verbatim.
    """
    digest = hashlib.sha256(f"{timestamp}\n{text}".encode()).hexdigest()
    return digest[:8]


def _normalize_handle(raw: str) -> str:
    """Normalise a caller-supplied handle before matching against one.

    The model reads `[a1b2c3d4] 2026-04-28 — ...` off the rendered prompt
    and will often echo the brackets back. Strip whitespace, drop one
    layer of surrounding `[ ]` if present, and lowercase. Matching is
    exact 8-character equality only — there is no prefix matching.
    """
    h = raw.strip()
    if len(h) > 2 and h.startswith("[") and h.endswith("]"):
        h = h[1:-1].strip()
    return h.lower()


@contextmanager
def _open_locked(path: Path, mode: str):
    """Open ``path`` for I/O in ``mode``, serialised by an exclusive lock
    held on a sidecar ``<name>.lock`` file — never on ``path`` itself.

    Locking the target file directly breaks the moment a writer replaces
    that file out from under an open descriptor (see ``_locked_rewrite``,
    which every live-file writer uses instead of this function). The
    archive file is only ever appended to, never replaced, but it goes
    through the same sidecar scheme so the two locking paths can't
    disagree, and so a lock on the archive never collides with a lock on
    a same-named live file.

    The lock is process-level via ``fcntl.flock`` — sufficient for the
    single-host deployment. If we ever go multi-host, swap for a DB row
    or a coordination service.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        content_handle = open(path, mode)
        try:
            yield content_handle
        finally:
            content_handle.close()
    finally:
        lock_handle.close()


def _current_umask() -> int:
    """Read the process umask without leaving it changed. ``os.umask``
    has no read-only form — you have to set it to read the old value."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _default_create_mode() -> int:
    """Permission bits for a notes file that doesn't exist yet: the
    conventional non-executable default minus the process umask — what
    a plain ``open(path, "w")`` would have produced."""
    return 0o666 & ~_current_umask()


def _write_atomic(path: Path, text: str, mode: int) -> None:
    """Write ``text`` to ``path`` atomically: a same-directory temp file,
    chmod'd to ``mode``, fsynced, then ``os.replace``'d over ``path``.

    Called only from ``_locked_rewrite``'s exit — every writer assembles
    its new whole-file content inside that held lock and hands it here;
    nothing calls this with text assembled outside the lock.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _ensure_trailing_newline(text: str) -> str:
    """Return ``text`` with a trailing newline, unless it's empty.

    This is the line-boundary invariant every writer in this module
    depends on: text that already satisfies it can be concatenated onto
    (another line appended, or more text written after it) without the
    last existing line welding into what follows. An empty string stays
    empty — there's no line to terminate, and turning "no file" or "no
    notes left" into a lone blank line would be its own corruption.
    """
    if not text:
        return text
    return text if text.endswith("\n") else text + "\n"


@dataclass(frozen=True)
class _Match:
    """One resolved handle lookup: which live line it points at, the
    parsed note there, and how many live lines matched in total.

    ``duplicates`` is normally 1. It is only >1 for a hand-edited pair of
    bullets sharing an identical timestamp AND text — those parse to the
    identical handle, and the module invites hand-editing, so this is a
    real shape rather than a defect to refuse against. The caller acts on
    the first match in file order and reports the count; after the write
    the acted-on note's text or timestamp has changed, so its handle
    changes too and the pair is unique again.
    """

    index: int
    note: Note
    duplicates: int


class _RewriteContext:
    """Handle for one locked read -> resolve -> rewrite cycle.

    ``existing_text``/``lines`` are the file as read *inside* the lock —
    the only state ``resolve`` may use. ``existing_text`` is normalised
    to end in a trailing newline (if it holds any content at all) before
    ``lines`` is derived from it, so every line in ``lines`` is properly
    terminated even when the file on disk was hand-edited or otherwise
    left without one. That is what lets a writer concatenate new content
    directly onto ``existing_text`` (or onto ``"".join(lines)``) without
    re-deriving the boundary itself — ``_rotate_to_fit`` in particular
    relies on this rather than re-implementing it.

    Assign ``.text`` before the ``with`` block exits; that whole-file
    text is what gets written (also normalised to end in a trailing
    newline, on the way out — see ``_locked_rewrite``). Leaving ``.text``
    as ``None`` (the target wasn't found, or nothing needs to change)
    writes nothing — the file is left untouched.
    """

    def __init__(self, existing_text: str):
        self.existing_text = _ensure_trailing_newline(existing_text)
        self.lines: list[str] = self.existing_text.splitlines(keepends=True)
        self.text: str | None = None

    def resolve(self, handle: str) -> _Match | None:
        """Find the first live line whose content handle equals
        ``handle`` (already normalised by the caller — see
        ``_normalize_handle``), among the lines read inside this lock.

        Returns a ``_Match`` naming the first line found plus how many
        lines matched in total, or ``None`` if no live line carries this
        handle — whether because it was never live, was already deleted,
        was rewritten (an update changes the text and/or timestamp, so
        it changes the handle too), or was rotated to the archive. This
        is the compare-and-swap: a caller holding a stale handle is
        refused loudly instead of silently landing on whatever now
        occupies its old position.

        This is the only supported way to target a note for update or
        delete — there is no free function that resolves against a list
        read outside the lock.
        """
        matches = [
            (i, parsed)
            for i, ln in enumerate(self.lines)
            if (parsed := _parse_line(ln)) is not None and parsed.handle == handle
        ]
        if not matches:
            return None
        index, note = matches[0]
        return _Match(index=index, note=note, duplicates=len(matches))


@contextmanager
def _locked_rewrite(path: Path, *, _after_read_hook=None):
    """Hold the sidecar lock for one read -> resolve -> rewrite cycle.

    Reads and parses the live file *inside* the lock and yields a
    ``_RewriteContext`` built from that read — every writer resolves its
    target and assembles its new content against that same read, never
    against a separately-read copy. The read is normalised to end in a
    trailing newline before it reaches the context (see
    ``_RewriteContext``), and ``ctx.text`` is normalised the same way on
    the way out — so a whole-file text a writer assembles by
    concatenating onto ``ctx.existing_text`` can never weld its last
    existing line into what got appended, on either side of the round
    trip. On exit (still holding the lock), if ``ctx.text`` was assigned,
    it is written atomically: a same-directory temp file, chmod'd to the
    original file's mode (or ``0o666 & ~umask`` if the file is being
    created), fsynced, then ``os.replace``'d over the original. A
    reader — ``read_notes``, or a text editor with the file open —
    therefore never observes a partial file: only the whole old one or
    the whole new one.

    ``_after_read_hook``, if given, fires once the read has happened but
    before control returns to the ``with`` block — a seam for tests that
    need to force a concurrent writer to overlap this held lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        mode = (
            stat.S_IMODE(path.stat().st_mode)
            if path.exists()
            else _default_create_mode()
        )
        if _after_read_hook is not None:
            _after_read_hook()
        ctx = _RewriteContext(existing_text)
        yield ctx
        if ctx.text is not None:
            _write_atomic(path, _ensure_trailing_newline(ctx.text), mode)
    finally:
        lock_handle.close()


def _parse_line(line: str) -> Note | None:
    """Parse one bullet. Returns None for blank/non-bullet lines so the
    file can hold human-edited prose without breaking the read path.

    ``position`` is always -1 here — this function has no idea where in
    the file it was called from; callers that know the offset (``read_notes``,
    ``_RewriteContext.resolve``) fill it in themselves. ``handle`` is always
    correct, since it is derived purely from ``timestamp``/``text``.
    """
    raw = line.rstrip("\n")
    if not raw.startswith("- "):
        return None
    body = raw[2:]
    # Bullet shape: "<iso timestamp> — <text>". The em-dash is the
    # separator we always emit; tolerate hyphen as a hand-edit fallback.
    for sep in (" — ", " - "):
        idx = body.find(sep)
        if idx > 0:
            ts = body[:idx].strip()
            text = body[idx + len(sep):].strip()
            return Note(handle=_handle(ts, text), position=-1, timestamp=ts, text=text)
    # No separator — treat the whole thing as undated text.
    text = body.strip()
    return Note(handle=_handle("", text), position=-1, timestamp="", text=text)


def read_notes(path: Path | None = None) -> list[Note]:
    """Return all parsed notes from the live file, in on-disk (arrival)
    order — oldest first. Missing file = empty list.

    This is NOT a recency order. ``update_note`` rewrites a note's
    timestamp in place without moving its line, so file position and
    recency agree only until the first refinement — after that, the
    newest note by timestamp can sit anywhere in this list. A caller that
    wants newest-first should rank the result with ``recent_first()``,
    which is what ``render_for_prompt``, ``list_user_notes``, and
    ``daily_snapshot``'s ``user_notes`` all do, so the three model-facing
    surfaces cannot disagree about which note is newest.
    """
    p = path or _default_notes_path()
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        LOG.warning("Failed to read notes from %s: %s", p, e)
        return []
    notes: list[Note] = []
    for idx, raw_line in enumerate(text.splitlines()):
        parsed = _parse_line(raw_line)
        if parsed is not None:
            notes.append(Note(handle=parsed.handle, position=idx,
                               timestamp=parsed.timestamp, text=parsed.text))
    return notes


def _recency_key(n: Note) -> tuple[int, str, int]:
    """Sort key for ``recent_first``, meant to be used with ``reverse=True``.

    ``parsed_ok`` is 1 for a timestamp that actually parses as ISO-8601,
    0 for blank or malformed. With ``reverse=True`` that puts every
    validly-timestamped note ahead of every undated/malformed one — a
    naive ``(timestamp, position)`` key would instead sort an empty
    string *first* (empty < any real timestamp), promoting a hand-edited
    undated bullet to the very front of the prompt. Within each group,
    ``timestamp`` breaks ties by recency and ``position`` (the file
    offset at read time) breaks ties between identical timestamps by
    which was written later — the same signal ``reversed(file order)``
    used before this ranking existed.

    This is a *display* key only. Rotation shared it and popped the
    tail, which silently turned "sorts last in the prompt" into "is the
    first thing thrown away" for exactly the bullets nobody stamped;
    eviction ranks through ``_eviction_key`` instead.
    """
    parsed_ok = 0
    if n.timestamp:
        try:
            datetime.fromisoformat(n.timestamp)
            parsed_ok = 1
        except ValueError:
            parsed_ok = 0
    return (parsed_ok, n.timestamp, n.position)


def recent_first(items: list[Note]) -> list[Note]:
    """Rank notes newest-first by timestamp, tie-broken by file position.

    The one ranking shared by ``render_for_prompt``, ``list_user_notes``,
    and ``daily_snapshot``'s ``user_notes`` — before this, all three
    derived "recent" from on-disk position, which stops being true the
    moment ``update_note`` refreshes a note's timestamp in place without
    moving its line (an in-place refinement leaves file order and
    recency order disagreeing, and a 4 KB rotation that evicts by
    position then archives the freshest note first). A blank or
    unparseable timestamp sorts LAST — see ``_recency_key``.

    Display only: ``_rotate_to_fit`` used to reuse this ranking to pick
    what to evict and no longer does — see ``_eviction_order``.
    """
    return sorted(items, key=_recency_key, reverse=True)


def _eviction_key(n: Note) -> tuple[int, str, int]:
    """Sort key for ``_eviction_order``, meant to be used with
    ``reverse=True`` — ``_recency_key`` with its ``parsed_ok`` term
    flipped, and nothing else changed.

    The flip is the whole point. Ranking undated bullets last is right
    for the prompt and exactly backwards for eviction: it made a line a
    human typed by hand the first casualty of the 4 KB cap, ahead of a
    stamped note years older, because rotation pops the tail of the
    ranking it is given. Here a blank or malformed timestamp sorts
    FIRST, i.e. most protected — a bullet nobody stamped carries no
    evidence of its age, and the caller cannot claim it is the oldest
    thing in the file. It stays evictable as a last resort (a file of
    nothing but undated bullets must still be able to shed weight), and
    within each group the ``(timestamp, position)`` tie-break is
    ``_recency_key``'s, so eviction among dated bullets is unchanged.
    """
    parsed_ok, timestamp, position = _recency_key(n)
    return (1 - parsed_ok, timestamp, position)


def _eviction_order(items: list[Note]) -> list[Note]:
    """Rank notes most-protected-first for rotation: the tail is what
    ``_rotate_to_fit`` evicts next. Undated/malformed bullets lead
    (evicted last), then dated bullets newest-first, so the tail is the
    oldest bullet that actually carries a timestamp.

    Deliberately not ``recent_first`` — see ``_eviction_key``. Display
    order does not move with it.
    """
    return sorted(items, key=_eviction_key, reverse=True)


def render_for_prompt(path: Path | None = None) -> str:
    """Render the notes for inclusion in a system prompt.

    Returns an empty string when there are no notes (caller can skip the
    section heading). Otherwise returns one bullet per line, ranked
    newest-first by ``recent_first`` — timestamp descending, tie-broken
    by on-disk position — so the order shown here, the order
    ``list_user_notes`` returns, and the order ``daily_snapshot``'s
    ``user_notes`` carries all agree. (Before this ranking existed,
    "newest first" meant reversed file order, which broke the moment any
    note was refined — the header's "prefer the newer note" rule then
    resolved backwards.)

    Each line carries the note's content ``handle`` as a ``[prefix]``, not
    a raw file line index — a line's position is not a stable identity
    (see ``Note.position``), so ``update_user_note`` / ``delete_user_note``
    address a note by this handle instead, and it keeps resolving
    correctly even after an unrelated note is deleted or the file is
    rotated. The date rides alongside the handle so the model can apply
    the "prefer the newer note" rule from evidence it can see, rather than
    trusting an ordering it has no way to verify.
    """
    notes = recent_first(read_notes(path))
    if not notes:
        return ""
    lines = []
    for n in notes:
        if not n.text:
            continue
        date_part = n.timestamp[:10] if n.timestamp else "undated"
        lines.append(f"[{n.handle}] {date_part} — {n.text}")
    return "\n".join(lines)


def _live_handles(lines: list[str], skip_index: int | None = None) -> set[str]:
    """Every content handle currently live in ``lines``, optionally
    excluding the line at ``skip_index``.

    ``skip_index`` is for a writer replacing a line in place: the line
    being rewritten is not a competitor with itself, and counting it
    would force a pointless re-stamp on an update that changes nothing.
    """
    return {
        parsed.handle
        for i, ln in enumerate(lines)
        if i != skip_index and (parsed := _parse_line(ln)) is not None
    }


def _stamp_without_collision(existing_handles: set[str], text: str) -> str:
    """Stamp ``text`` with now() at whole-second resolution, stepping a
    second forward at a time until ``_handle(timestamp, text)`` is not
    already in ``existing_handles``. Returns the ISO timestamp.

    Never manufacture a handle collision. Two notes with an identical
    (timestamp, text) pair parse to the identical handle — the shape
    update_note/delete_note tolerate as a hand-edited duplicate, not one
    these tools should create on their own. Both writers share this: the
    in-app paths to a collision are two save_user_note calls with
    identical text inside the same wall-clock second, and an
    update_user_note whose (now, new text) lands on a live bullet's
    (timestamp, text) — which needs two updates in one second, or a
    rewrite onto text a bullet stamped this second already carries.

    Re-stamps rather than refusing, so "make these two notes say the same
    thing" stays a legal request. The consequence, documented in the four
    note docs: two live bullets may carry identical *text* under two
    distinct handles, each independently addressable. What cannot happen
    is one handle addressing two bullets.

    Must be called inside the writer's held lock, against the lines read
    inside it — a handle set built from a read outside the lock is a set
    of handles another writer may already have invalidated.
    """
    ts_dt = datetime.now().replace(microsecond=0)
    ts = ts_dt.isoformat()
    while _handle(ts, text) in existing_handles:
        ts_dt += timedelta(seconds=1)
        ts = ts_dt.isoformat()
    return ts


def append_note(text: str, path: Path | None = None) -> Note:
    """Append a single note to the live file. Newline-folds the input so a
    multi-line message doesn't break the bullet structure. If appending
    would push the file past LIVE_FILE_MAX_BYTES, oldest bullets are
    rotated to the archive file first — and if that archive write fails,
    nothing is evicted and the file is written over budget instead. The
    cap bounds prompt size; it is not a licence to destroy a preference
    that has nowhere else to go.
    """
    text = " ".join(text.split())  # collapse all whitespace to single spaces
    if not text:
        raise ValueError("note text is empty after whitespace normalization")
    if len(text) > 800:
        # Prevent runaway agents from saving novel-length notes. 800 chars
        # is room for a long, specific preference but not a dissertation.
        text = text[:800].rstrip() + "…"

    p = path or _default_notes_path()

    with _locked_rewrite(p) as ctx:
        # Stamped against the lines read inside this lock, so the append
        # can never mint a handle a live bullet already carries — see
        # _stamp_without_collision. update_note shares the guard.
        ts = _stamp_without_collision(_live_handles(ctx.lines), text)
        new_line = f"- {ts} — {text}\n"

        # ctx.existing_text is already newline-normalised (see
        # _RewriteContext), so appending new_line directly can never
        # weld it into a bare-last-line file.
        existing = ctx.existing_text
        candidate = existing + new_line
        if len(candidate.encode("utf-8")) > LIVE_FILE_MAX_BYTES:
            kept, rotated, _protected = _rotate_to_fit(existing, new_line)
            # Archive before the live replace: if the process dies
            # between the two, the rotated note exists twice (archive
            # + live) rather than zero times — duplication recovers,
            # loss doesn't. And if the archive can't be written at all,
            # keep the evicted bullets live: the cap bounds prompt size,
            # it doesn't license deletion, and _rotate_to_fit already
            # takes exactly this way out for unevictable prose.
            if rotated and not _append_archive(rotated, _archive_path(p)):
                LOG.warning(
                    "Archive write failed — keeping %d rotated byte(s) live "
                    "and writing %d bytes over budget (cap %d) rather than "
                    "destroying them.",
                    len(rotated.encode("utf-8")),
                    len(candidate.encode("utf-8")), LIVE_FILE_MAX_BYTES,
                )
                ctx.text = candidate
                final_text = candidate
            else:
                ctx.text = kept
                final_text = kept
        else:
            ctx.text = candidate
            final_text = candidate
    LOG.info("Saved user note (%d chars)", len(text))
    # The appended bullet is the last line of the file; its position
    # (matching how read_notes/update_note/delete_note count via
    # splitlines()) is the count of file lines minus one. Position is a
    # hint only — the handle is what a caller actually addresses it by.
    new_position = len(final_text.splitlines()) - 1
    return Note(handle=_handle(ts, text), position=new_position, timestamp=ts, text=text)


def _rotate_to_fit(
    existing: str, new_line: str = "", protected_index: int | None = None,
) -> tuple[str, str, int | None]:
    """Drop bullets from ``existing``, oldest dated first, until
    ``existing + new_line`` fits the cap. Returns (kept_text,
    rotated_text, final_protected_index) where rotated_text contains the
    dropped lines, in the order they were evicted, for archiving, and
    final_protected_index is ``protected_index`` adjusted for every
    eviction that shifted it (or ``None`` if ``protected_index`` was
    ``None``) — the caller's cheap way to keep addressing the same line
    across a rotation without re-deriving its position by content.

    Eviction ranks by ``_eviction_order`` and pops from the tail, not by
    file position — a position-based eviction (the old ``lines.pop(0)``)
    archives the *freshest* note first the moment any note has been
    refreshed out of file order (Repro 7): ``update_note`` rewrites a
    timestamp in place without moving the line, so "oldest line" and
    "oldest note" stop being the same question after the first
    refinement. That ordering is this function's own and is *not*
    ``recent_first``: an undated or malformed bullet sorts last for
    display and most-protected for eviction, so a line a human typed by
    hand is the last bullet to go rather than the first — it carries no
    evidence of its age, and nothing licenses calling it the oldest
    thing in the file. A non-bullet line (free prose the file's own
    docstring invites) carries no timestamp and is never a candidate at
    all — only parsed bullets are evicted. If every bullet is gone and
    the file (now pure prose, or already minimal) still exceeds the cap,
    the loop stops and the write proceeds over budget rather than
    spinning forever or discarding content that isn't a note at all.

    ``new_line`` defaults to empty: ``append_note`` calls this with the
    incoming bullet split out as ``new_line`` (it isn't part of ``lines``
    yet), while ``update_note`` calls it with the *already-assembled*
    whole-file text as ``existing`` and no separate ``new_line`` — the
    just-refreshed bullet is already one of ``lines`` in that case.

    ``protected_index`` names a line in ``existing`` (by its index at
    call time) that is never a candidate for eviction, no matter what
    ``_eviction_order`` would otherwise rank it. ``update_note`` passes
    the index of the bullet it just rewrote: that bullet's brand-new
    timestamp *usually* keeps that ordering from picking it as
    oldest, but ``_eviction_key`` tie-breaks equal timestamps by
    position, and a same-wall-clock-second update racing another note's
    write — or a hand-edited bullet carrying a future timestamp — can
    still make the just-rewritten line the tail of that ordering.
    Excluding it from the candidate set is what makes "never evicted by
    its own update" true in every case rather than true only when no
    other note ties its timestamp. ``append_note`` passes ``None``: the
    bullet it is adding is carried in ``new_line``, not in ``lines``, so
    it never needs protecting from this same loop.

    Relies on ``existing`` already ending in a trailing newline (or
    being empty) — every caller passes either ``ctx.existing_text`` or a
    whole-file text built from it, both of which ``_RewriteContext``
    guarantees are newline-terminated. That's what lets the joins below
    happen cleanly instead of welding the last kept line into
    ``new_line``: this function establishes no boundary of its own
    because ``_locked_rewrite`` already established one for it.
    """
    lines = existing.splitlines(keepends=True)
    rotated: list[str] = []
    while True:
        candidate = "".join(lines) + new_line
        if len(candidate.encode("utf-8")) <= LIVE_FILE_MAX_BYTES:
            break
        bullets = [
            Note(handle=parsed.handle, position=i,
                 timestamp=parsed.timestamp, text=parsed.text)
            for i, ln in enumerate(lines)
            if i != protected_index and (parsed := _parse_line(ln)) is not None
        ]
        if not bullets:
            LOG.warning(
                "Notes file still exceeds %d bytes with no evictable "
                "bullet remaining (%d bytes of non-bullet content); "
                "writing over budget rather than discarding it.",
                LIVE_FILE_MAX_BYTES, len(candidate.encode("utf-8")),
            )
            break
        victim = _eviction_order(bullets)[-1]
        popped_index = victim.position
        rotated.append(lines.pop(popped_index))
        if protected_index is not None and popped_index < protected_index:
            # Everything after the popped line shifted left by one — keep
            # pointing at the same (still-unevicted) protected line.
            protected_index -= 1
    kept = "".join(lines) + new_line
    return kept, "".join(rotated), protected_index


def _append_archive(text: str, archive_path: Path) -> bool:
    """Append rotated content to the archive file. Returns whether the
    append landed — ``False`` means the rotated content now exists nowhere
    but in the caller's hands, and the caller must keep it live.

    Failures are logged rather than raised: an unwritable archive is a
    degraded state, not a reason for every note write to start failing,
    and the casualty of raising here would be the user's brand-new
    preference, which had nothing to do with the failure. What the
    ``None`` this used to return could not tell a caller is *which* of the
    two happened — so both callers truncated the live file either way, and
    a read-only archive destroyed the evicted preferences outright.

    Establishes the line boundary on *both* sides of the join: the text
    being appended (via ``_ensure_trailing_newline``, same as before),
    and — the file it appends *to*. Unlike the live file, the archive is
    never rewritten through ``_locked_rewrite``, so nothing upstream
    normalises what's already on disk; a previous run that welded a line
    (or a hand-edited archive with no trailing newline) would otherwise
    weld the very first archived line of *this* rotation onto it too.
    """
    try:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with _open_locked(archive_path, "a") as h:
            needs_boundary = False
            if archive_path.exists() and archive_path.stat().st_size > 0:
                with open(archive_path, "rb") as rf:
                    rf.seek(-1, os.SEEK_END)
                    needs_boundary = rf.read(1) != b"\n"
            if needs_boundary:
                h.write("\n")
            h.write(_ensure_trailing_newline(text))
    except OSError as e:
        LOG.warning("Failed to write archive %s: %s", archive_path, e)
        return False
    return True


def update_note(handle: str, new_text: str, path: Path | None = None) -> tuple[Note, int] | None:
    """Replace the bullet whose content handle is ``handle`` with
    ``new_text`` (timestamp refreshed to now). Returns ``(new_note,
    duplicates)`` or ``None`` if no live line's handle matches — a stale
    handle (the target was itself updated, deleted, or rotated to the
    archive since the caller read it) is the loud failure the index bug
    never gave: nothing is written, and the caller is told to re-read
    rather than being silently pointed at a different note. ``handle`` is
    normalised (stripped, debracketed, lowercased) before matching, and
    matched by exact value only — no prefix matching.

    ``duplicates`` is the number of live lines that matched before the
    rewrite, normally 1; see ``_Match``/``_RewriteContext.resolve`` for
    the >1 case. Used for in-place updates so a refined preference
    doesn't pile a duplicate onto the file. A ``duplicates`` above 1 is
    now necessarily a hand-edit: this function used to stamp outside the
    lock with no handle check, so two updates to the same text inside one
    wall-clock second minted the very shape ``_Match`` exists to tolerate.
    The stamp is taken inside the lock and through
    ``_stamp_without_collision``, against every live handle except the
    line being replaced — so a same-second rewrite is stamped one or more
    seconds forward rather than colliding, exactly as ``append_note``
    already did.

    Rewriting a short note into a much longer one can push the live file
    past ``LIVE_FILE_MAX_BYTES`` — unlike ``append_note``, this rewrites a
    line in place rather than adding one, so nothing about the write
    itself would ever trip a size check unless this function makes one.
    So, still inside the same held lock this rewrite happened in, the
    result is size-checked the same way ``append_note`` checks a
    candidate append: if it's over budget, the oldest bullets *by
    timestamp* are rotated to the archive until it fits. The just-updated
    note is stamped with ``now()`` moments before this check, which keeps
    ``recent_first`` from ranking it oldest in the ordinary case — but a
    second update landing in the same wall-clock second, or a hand-edited
    bullet carrying a future timestamp, ties that ranking, and
    ``_recency_key`` breaks ties by position, which does not favour the
    just-rewritten line. So the rewritten line's index is passed to
    ``_rotate_to_fit`` as ``protected_index``, which excludes it from the
    eviction candidates outright: the just-updated note is never the
    thing evicted by its own update, in every case, not only the case
    where no other note's timestamp ties it.

    If the archive write fails, nothing is evicted and the over-budget
    text is written as-is — same as ``append_note``, and it matters more
    here: one capacity-tripping update rotates as many bullets as the new
    text costs. The returned ``position`` then still points at the
    rewritten line, because no line ahead of it moved.
    """
    new_text = " ".join(new_text.split())
    if not new_text:
        raise ValueError("new note text is empty after whitespace normalization")
    if len(new_text) > 800:
        new_text = new_text[:800].rstrip() + "…"

    p = path or _default_notes_path()
    if not p.exists():
        return None

    handle = _normalize_handle(handle)
    result: tuple[Note, int] | None = None
    with _locked_rewrite(p) as ctx:
        match = ctx.resolve(handle)
        if match is None:
            return None
        lines = ctx.lines
        # Stamped inside the lock, against the lines read inside it, and
        # against every live handle except the one on the line being
        # replaced — see _stamp_without_collision and _live_handles.
        ts = _stamp_without_collision(_live_handles(lines, match.index), new_text)
        # Preserve trailing newline character of the original line so the
        # file shape stays consistent.
        had_newline = lines[match.index].endswith("\n")
        lines[match.index] = f"- {ts} — {new_text}" + ("\n" if had_newline else "")
        candidate = "".join(lines)
        new_position = match.index

        if len(candidate.encode("utf-8")) > LIVE_FILE_MAX_BYTES:
            kept, rotated, protected_after = _rotate_to_fit(
                candidate, protected_index=new_position,
            )
            # Archive before the live replace, and keep the evicted
            # bullets live if the archive can't be written — see
            # append_note for both halves of why.
            if rotated and not _append_archive(rotated, _archive_path(p)):
                LOG.warning(
                    "Archive write failed — keeping %d rotated byte(s) live "
                    "and writing %d bytes over budget (cap %d) rather than "
                    "destroying them.",
                    len(rotated.encode("utf-8")),
                    len(candidate.encode("utf-8")), LIVE_FILE_MAX_BYTES,
                )
                ctx.text = candidate
                # Nothing was popped on this path, so new_position stays
                # match.index — protected_after is short by however many
                # lines the rotation WOULD have removed ahead of it.
            else:
                ctx.text = kept
                # protected_index was passed as an int (new_position), so
                # _rotate_to_fit always returns an int back here too.
                assert protected_after is not None
                new_position = protected_after
        else:
            ctx.text = candidate

        new_note = Note(handle=_handle(ts, new_text), position=new_position,
                         timestamp=ts, text=new_text)
        result = (new_note, match.duplicates)
    return result


def delete_note(handle: str, path: Path | None = None) -> int | None:
    """Remove the first live bullet whose content handle is ``handle``.

    Returns the number of live lines that matched *before* the removal
    (normally 1; see ``_Match`` for the duplicate case), or ``None`` if
    no live line's handle matches — whether because it never existed,
    was already deleted, was rewritten by an update, or was rotated to
    the archive. ``handle`` is normalised the same way as ``update_note``.
    """
    p = path or _default_notes_path()
    if not p.exists():
        return None

    handle = _normalize_handle(handle)
    result: int | None = None
    with _locked_rewrite(p) as ctx:
        match = ctx.resolve(handle)
        if match is None:
            return None
        lines = ctx.lines
        del lines[match.index]
        ctx.text = "".join(lines)
        result = match.duplicates
    return result
