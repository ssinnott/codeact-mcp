"""Writing JSON Lines files without leaving a half-written file behind."""

from __future__ import annotations

import contextlib
import json
import os
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
            "code": "_wp.read_text()",
            "note": "the failed write left the file exactly as it was — id 4 was "
            "never committed, which is the point of the temp-file-and-rename",
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
        this helper commits only at the end), or the data is a single object
        rather than a stream of records (use json.dump).

    Args:
        path: Destination file path. It is replaced atomically. Its parent
            directory must already exist — the temp file is created there, which
            is what makes the final rename atomic.
        records: The records to write, one JSON object per line. Any iterable,
            including a generator, and it is consumed exactly once. Each item
            must be JSON-serializable; dicts are the normal case but any
            serializable value works.
        append: Add to the file's existing content instead of replacing it. The
            existing content is copied into the temp file first, so an append
            reads and rewrites the whole file and needs it to fit in memory. A
            missing file is created either way. A final newline is added to the
            old content if it lacked one, so records cannot run together.

    Returns:
        the count of records actually written — the number of new lines added,
        not the total line count of the file after an append.

    Raises:
        TypeError: a record is not JSON-serializable (a set, a datetime, an
            arbitrary object). The target file is left untouched; convert the
            offending field to a string or number and call again.
        FileNotFoundError: the parent directory of `path` does not exist.
            Create it first — this helper will not create directories.
        PermissionError: the parent directory is not writable. Note it is the
            directory, not the file, that must be writable, because the write
            goes through a new temp file there.

    Preconditions:
        The parent directory of `path` must exist and be writable, and have room
        for a second copy of the data while the write is in flight.

    Notes:
        Everything is staged in a temp file beside the target and moved into
        place with one os.replace, so the path holds either the old content or
        the whole new content, never a truncated mix — true whether the process
        dies mid-write, a record fails to serialize, or the source generator
        raises. Output is UTF-8 with non-ASCII characters written literally
        rather than \\u-escaped, and each line is compact JSON with no newlines
        inside it, so the file is safe to read back line by line.
    """
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if append and target.exists():
                existing = target.read_text(encoding="utf-8")
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                fh.write(existing)
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return count
