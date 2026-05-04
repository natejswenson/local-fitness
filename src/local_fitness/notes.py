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
the next prompt build picks up the change. Concurrent writers are
serialised by ``fcntl.flock`` so two chat sessions can't corrupt the
file. A 4 KB live cap keeps prompt context bounded — older bullets
overflow to ``user_notes.archive.md`` rather than getting lost.
"""
from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger(__name__)

# 4 KB live-file budget — keeps the system-prompt injection bounded.
# Tested: ~40-50 typical preference bullets fit, plenty for one user.
LIVE_FILE_MAX_BYTES = 4096


def _default_notes_path() -> Path:
    """Resolve the notes file path. Honors LOCAL_FITNESS_NOTES_PATH; falls
    back to the same data dir convention as the SQLite DB."""
    override = os.environ.get("LOCAL_FITNESS_NOTES_PATH")
    if override:
        return Path(override)
    data_override = os.environ.get("LOCAL_FITNESS_DATA_DIR")
    base = Path(data_override) if data_override else Path.home() / "localrepo" / "local-fitness" / "data"
    return base / "user_notes.md"


def _archive_path(live_path: Path) -> Path:
    return live_path.with_name(live_path.stem + ".archive" + live_path.suffix)


@dataclass(frozen=True)
class Note:
    line: int  # 0-indexed position in the live file (stable until next write)
    timestamp: str  # ISO-8601 second-precision
    text: str


def _open_locked(path: Path, mode: str):
    """Open ``path`` with an exclusive lock held for the lifetime of the
    file handle. Caller is responsible for closing.

    The lock is process-level via ``fcntl.flock`` — sufficient for the
    single-host deployment. If we ever go multi-host, swap for a DB row
    or a coordination service.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, mode)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        handle.close()
        raise
    return handle


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

    handle = _open_locked(p, "a+")
    try:
        handle.seek(0)
        existing = handle.read()
        candidate = existing + (new_line if existing.endswith("\n") or not existing else "\n" + new_line)
        if len(candidate.encode("utf-8")) > LIVE_FILE_MAX_BYTES:
            kept, rotated = _rotate_to_fit(existing, new_line)
            if rotated:
                _append_archive(rotated, _archive_path(p))
            handle.seek(0)
            handle.truncate()
            handle.write(kept)
        else:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(new_line)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    LOG.info("Saved user note (%d chars)", len(text))
    return Note(line=-1, timestamp=ts, text=text)


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
    handle = _open_locked(p, "r+")
    try:
        text = handle.read()
        lines = text.splitlines(keepends=True)
        if line_index < 0 or line_index >= len(lines):
            return None
        if _parse_line(lines[line_index]) is None:
            return None
        ts = datetime.now().replace(microsecond=0).isoformat()
        # Preserve trailing newline character of the original line so the
        # file shape stays consistent.
        had_newline = lines[line_index].endswith("\n")
        lines[line_index] = f"- {ts} — {new_text}" + ("\n" if had_newline else "")
        handle.seek(0)
        handle.truncate()
        handle.write("".join(lines))
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    return Note(line=line_index, timestamp=ts, text=new_text)


def delete_note(line_index: int, path: Path | None = None) -> bool:
    """Remove the bullet at ``line_index`` (0-indexed against the live
    file's lines, matching ``Note.line``). Returns True if a line was
    removed, False if the index doesn't point at a bullet.
    """
    p = path or _default_notes_path()
    if not p.exists():
        return False
    handle = _open_locked(p, "r+")
    try:
        text = handle.read()
        lines = text.splitlines(keepends=True)
        if line_index < 0 or line_index >= len(lines):
            return False
        if _parse_line(lines[line_index]) is None:
            # Don't let callers delete arbitrary non-bullet lines.
            return False
        del lines[line_index]
        handle.seek(0)
        handle.truncate()
        handle.write("".join(lines))
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    return True
