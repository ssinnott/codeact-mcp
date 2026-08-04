"""Writing JSON Lines files without leaving a half-written file behind."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path

from codeact import helper


@helper(
    job="mutate",
    domains=["data", "fs"],
    side_effects="filesystem",
    examples=[
        {
            "setup": (
                "import pathlib, tempfile\n"
                "_wp = pathlib.Path(tempfile.mkdtemp()) / 'out.jsonl'"
            ),
            "code": "write_jsonl(str(_wp), [{'id': 1, 'ok': True}, {'id': 2, 'ok': False}])",
            "note": "_wp is a path in a fresh temp dir; the return value is the "
            "number of records written",
        },
        {
            "code": "_wp.read_text()",
            "note": "one compact JSON object per line, every line newline-terminated",
        },
        {
            "code": "write_jsonl(str(_wp), [{'id': 3}], append=True)",
            "note": "append=True keeps the two existing records and adds one",
        },
        {
            "code": "_wp.read_text()",
            "note": "the appended record follows the originals",
        },
        {
            "code": "write_jsonl(str(_wp), [{'id': 4}, {'when': object()}])",
            "note": "a record that will not serialize aborts the whole write",
            "raises": True,
        },
        {
            "code": "write_jsonl(str(_wp), [{'score': float('nan')}])",
            "note": "NaN and +/-Infinity have no JSON spelling, so they are "
            "refused rather than written as bare NaN/Infinity tokens that "
            "strict readers reject — pass None or a string instead",
            "raises": True,
        },
        {
            "code": "_wp.read_text()",
            "note": "neither failed write committed anything: the file still "
            "holds ids 1-3, which is the point of the temp-file-and-rename",
        },
        {
            "setup": "_wp.chmod(0o640)\nwrite_jsonl(str(_wp), [{'id': 5}])",
            "code": "oct(_wp.stat().st_mode & 0o777)",
            "note": "after chmod 0o640 and one more write_jsonl (which replaced "
            "the contents with a single id-5 record), the file still has mode "
            "0640 — a rewrite keeps the destination's "
            "permission bits rather than resetting them to the staging file's 0600",
        },
    ],
)
def write_jsonl(path: str, records: Iterable[dict], *, append: bool = False) -> int:
    """Write records to a JSON Lines file so readers never see a partial file.

    Use when: you are producing a .jsonl/.ndjson file that something else will
        read — a later step, another process, a re-run of the same job — and a
        half-written file would be mistaken for a complete one.
    Don't use when: you are streaming records over a long-running job and want
        each one durable as it is produced (open the file yourself and append;
        this helper commits only at the end), the data is a single object rather
        than a stream of records (use json.dump), or several writers append to
        one file concurrently (see `append` — this is a read-and-rewrite, not an
        O_APPEND write, so concurrent appenders overwrite each other).

    Args:
        path: Destination file path. It is replaced atomically. Its parent
            directory must already exist — the temp file is created there, which
            is what makes the final rename atomic.
        records: The records to write, one JSON object per line. Any iterable,
            including a generator, and it is consumed exactly once. Each item
            must be JSON-serializable; dicts are the normal case but any
            serializable value works. Floats must be finite: NaN and Infinity
            are rejected, not written.
        append: Add to the file's existing content instead of replacing it. This
            is still atomic — the existing content is copied into the temp file
            first and the whole result is renamed into place — but that means an
            append reads and rewrites the whole file and needs it to fit in
            memory, and a second writer appending at the same time will clobber
            the first one's records. The old content is copied through verbatim
            and is not re-validated. A missing file is created either way. A
            final newline is added to the old content if it lacked one, so
            records cannot run together.

    Returns:
        the count of records actually written — the number of new lines added,
        not the total line count of the file after an append.

    Raises:
        TypeError: a record is not JSON-serializable (a set, a datetime, an
            arbitrary object). The target file is left untouched; convert the
            offending field to a string or number and call again.
        ValueError: a record contains a non-finite float — NaN, Infinity or
            -Infinity — which JSON cannot represent. The target file is left
            untouched; replace the value with None, a string, or a finite
            number. This is checked rather than emitted so the file is never
            left holding bare `NaN`/`Infinity` tokens that strict JSON parsers
            reject (Python's own json module would read them back happily,
            which is exactly what makes the corruption easy to miss).
        FileNotFoundError: the parent directory of `path` does not exist.
            Create it first — this helper will not create directories.
        PermissionError: the parent directory is not writable, or an existing
            target's permission bits could not be copied onto the replacement.
            Note it is the directory, not the file, that must be writable,
            because the write goes through a new temp file there.

    Preconditions:
        The parent directory of `path` must exist and be writable, and have room
        for a second copy of the data while the write is in flight.

    Notes:
        Everything is staged in a temp file beside the target and moved into
        place with one os.replace — in both modes, append included — so the path
        holds either the old content or the whole new content, never a truncated
        mix: true whether the process dies mid-write, a record fails to
        serialize, or the source generator raises. The data is fsynced but the
        directory entry is not, so a machine-level crash right after the rename
        can still leave the old content in place — never a mix, but do not read
        this as a durability guarantee. An existing target's
        permission bits are copied onto the replacement so a rewrite does not
        silently narrow them; ownership and ACLs are not copied, and a file this
        helper creates from scratch is owner-only (0600), so chmod it afterwards
        if it needs to be group- or world-readable. Output is UTF-8 with
        non-ASCII characters written literally rather than \\u-escaped, and each
        line is compact JSON with no newlines inside it, so the file is safe to
        read back line by line.
    """
    target = Path(path)
    try:
        existing_mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if append and existing_mode is not None:
                existing = target.read_text(encoding="utf-8")
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                fh.write(existing)
            for record in records:
                # allow_nan=False: the default emits bare NaN/Infinity, which is
                # not JSON, so a silent write here becomes someone else's parse
                # error later. Fail before anything is committed instead.
                fh.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        if existing_mode is not None:
            # mkstemp creates the staging file 0600; without this the rename
            # would quietly replace the target's mode with that.
            os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return count
