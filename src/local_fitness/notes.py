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
import logging
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
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
    line: int  # 0-indexed position in the live file (stable until next write)
    timestamp: str  # ISO-8601 second-precision
    text: str


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


class _RewriteContext:
    """Handle for one locked read -> resolve -> rewrite cycle.

    ``existing_text``/``lines`` are the file as read *inside* the lock —
    the only state ``resolve`` may use. Assign ``.text`` before the
    ``with`` block exits; that whole-file text is what gets written.
    Leaving ``.text`` as ``None`` (the target wasn't found, or nothing
    needs to change) writes nothing — the file is left untouched.
    """

    def __init__(self, existing_text: str):
        self.existing_text = existing_text
        self.lines: list[str] = existing_text.splitlines(keepends=True)
        self.text: str | None = None

    def resolve(self, line_index: int) -> Note | None:
        """Validate ``line_index`` against the lines read inside this
        lock and return the parsed note there, or None if the index is
        out of range or no longer points at a bullet.

        This is the only supported way to target a line for update or
        delete — there is no free function that resolves against a list
        read outside the lock.
        """
        if line_index < 0 or line_index >= len(self.lines):
            return None
        return _parse_line(self.lines[line_index])


@contextmanager
def _locked_rewrite(path: Path, *, _after_read_hook=None):
    """Hold the sidecar lock for one read -> resolve -> rewrite cycle.

    Reads and parses the live file *inside* the lock and yields a
    ``_RewriteContext`` built from that read — every writer resolves its
    target and assembles its new content against that same read, never
    against a separately-read copy. On exit (still holding the lock), if
    ``ctx.text`` was assigned, it is written atomically: a
    same-directory temp file, chmod'd to the original file's mode (or
    ``0o666 & ~umask`` if the file is being created), fsynced, then
    ``os.replace``'d over the original. A reader — ``read_notes``, or a
    text editor with the file open — therefore never observes a partial
    file: only the whole old one or the whole new one.

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
            _write_atomic(path, ctx.text, mode)
    finally:
        lock_handle.close()


def _parse_line(line: str) -> Note | None:
    """Parse one bullet. Returns None for blank/non-bullet lines so the
    file can hold human-edited prose without breaking the read path."""
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
            return Note(line=-1, timestamp=ts, text=text)
    # No separator — treat the whole thing as undated text.
    return Note(line=-1, timestamp="", text=body.strip())


def read_notes(path: Path | None = None) -> list[Note]:
    """Return all parsed notes from the live file, newest-first ordering
    matching the on-disk order. Missing file = empty list."""
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
            notes.append(Note(line=idx, timestamp=parsed.timestamp, text=parsed.text))
    return notes


def render_for_prompt(path: Path | None = None) -> str:
    """Render the notes for inclusion in a system prompt.

    Returns an empty string when there are no notes (caller can skip the
    section heading). Otherwise returns one bullet per line, newest-first.
    Includes the on-disk line index as a prefix so the model can reference
    a specific note by number when the user asks to update or remove one
    ("delete note 2", "replace the kindness note") — this is what powers
    the conversational management flow.
    """
    notes = list(reversed(read_notes(path)))  # newest first
    if not notes:
        return ""
    return "\n".join(f"[{n.line}] {n.text}" for n in notes if n.text)


def append_note(text: str, path: Path | None = None) -> Note:
    """Append a single note to the live file. Newline-folds the input so a
    multi-line message doesn't break the bullet structure. If appending
    would push the file past LIVE_FILE_MAX_BYTES, oldest bullets are
    rotated to the archive file first.
    """
    text = " ".join(text.split())  # collapse all whitespace to single spaces
    if not text:
        raise ValueError("note text is empty after whitespace normalization")
    if len(text) > 800:
        # Prevent runaway agents from saving novel-length notes. 800 chars
        # is room for a long, specific preference but not a dissertation.
        text = text[:800].rstrip() + "…"

    p = path or _default_notes_path()
    ts = datetime.now().replace(microsecond=0).isoformat()
    new_line = f"- {ts} — {text}\n"

    with _locked_rewrite(p) as ctx:
        existing = ctx.existing_text
        candidate = existing + (new_line if existing.endswith("\n") or not existing else "\n" + new_line)
        if len(candidate.encode("utf-8")) > LIVE_FILE_MAX_BYTES:
            kept, rotated = _rotate_to_fit(existing, new_line)
            if rotated:
                # Archive before the live replace: if the process dies
                # between the two, the rotated note exists twice (archive
                # + live) rather than zero times — duplication recovers,
                # loss doesn't.
                _append_archive(rotated, _archive_path(p))
            ctx.text = kept
            final_text = kept
        else:
            ctx.text = candidate
            final_text = candidate
    LOG.info("Saved user note (%d chars)", len(text))
    # The appended bullet is the last line of the file; its raw line index
    # (matching how read_notes/update_note/delete_note count via
    # splitlines()) is the count of file lines minus one.
    new_line_index = len(final_text.splitlines()) - 1
    return Note(line=new_line_index, timestamp=ts, text=text)


def _rotate_to_fit(existing: str, new_line: str) -> tuple[str, str]:
    """Drop oldest lines from ``existing`` until ``existing + new_line``
    fits the cap. Returns (kept_text, rotated_text) where rotated_text
    contains the dropped lines (in original order) for archiving.
    """
    lines = existing.splitlines(keepends=True)
    rotated: list[str] = []
    while lines:
        candidate = "".join(lines) + new_line
        if len(candidate.encode("utf-8")) <= LIVE_FILE_MAX_BYTES:
            break
        rotated.append(lines.pop(0))
    kept = "".join(lines) + new_line
    return kept, "".join(rotated)


def _append_archive(text: str, archive_path: Path) -> None:
    """Append rotated content to the archive file. Best-effort — failures
    are logged but don't block the live write."""
    try:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with _open_locked(archive_path, "a") as h:
            h.write(text)
            if not text.endswith("\n"):
                h.write("\n")
    except OSError as e:
        LOG.warning("Failed to write archive %s: %s", archive_path, e)


def update_note(line_index: int, new_text: str, path: Path | None = None) -> Note | None:
    """Replace the bullet at ``line_index`` with ``new_text`` (timestamp
    refreshed to now). Returns the new ``Note`` or None if the index
    doesn't point at a bullet. Used for in-place updates so a refined
    preference doesn't pile a duplicate onto the file.
    """
    new_text = " ".join(new_text.split())
    if not new_text:
        raise ValueError("new note text is empty after whitespace normalization")
    if len(new_text) > 800:
        new_text = new_text[:800].rstrip() + "…"

    p = path or _default_notes_path()
    if not p.exists():
        return None

    ts = datetime.now().replace(microsecond=0).isoformat()
    result: Note | None = None
    with _locked_rewrite(p) as ctx:
        if ctx.resolve(line_index) is None:
            return None
        lines = ctx.lines
        # Preserve trailing newline character of the original line so the
        # file shape stays consistent.
        had_newline = lines[line_index].endswith("\n")
        lines[line_index] = f"- {ts} — {new_text}" + ("\n" if had_newline else "")
        ctx.text = "".join(lines)
        result = Note(line=line_index, timestamp=ts, text=new_text)
    return result


def delete_note(line_index: int, path: Path | None = None) -> bool:
    """Remove the bullet at ``line_index`` (0-indexed against the live
    file's lines, matching ``Note.line``). Returns True if a line was
    removed, False if the index doesn't point at a bullet.
    """
    p = path or _default_notes_path()
    if not p.exists():
        return False

    removed = False
    with _locked_rewrite(p) as ctx:
        if ctx.resolve(line_index) is None:
            # Out of range, or no longer a bullet — don't let callers
            # delete arbitrary non-bullet lines.
            return False
        lines = ctx.lines
        del lines[line_index]
        ctx.text = "".join(lines)
        removed = True
    return removed
