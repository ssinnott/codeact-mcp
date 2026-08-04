"""Reading JSON Lines files."""

from __future__ import annotations

import json
from pathlib import Path

from codeact import helper


@helper(
    job="parse",
    domains=["data", "fs"],
    side_effects="filesystem",
    examples=[
        {
            "setup": (
                "import tempfile, pathlib\n"
                "_p = pathlib.Path(tempfile.mkdtemp()) / 'demo.jsonl'\n"
                "_p.write_text('{\"id\": 1, \"ok\": true}\\n\\n{\"id\": 2, \"ok\": false}\\n')"
            ),
            "code": "read_jsonl(str(_p))",
            "note": "_p holds two records separated by a blank line; blank lines are skipped by default",
        },
        {
            "code": "read_jsonl(str(_p), skip_blank=False)",
            "note": "same file, skip_blank=False: the blank line is now corruption, and JSONDecodeError names it",
            "raises": True,
        },
        {
            "setup": (
                "import tempfile, pathlib\n"
                "_empty = pathlib.Path(tempfile.mkdtemp()) / 'empty.jsonl'\n"
                "_empty.write_text('')"
            ),
            "code": "read_jsonl(str(_empty))",
            "note": "an empty file is not an error — it returns an empty list",
        },
        {
            "setup": (
                "import tempfile, pathlib\n"
                "_latin1 = pathlib.Path(tempfile.mkdtemp()) / 'latin1.jsonl'\n"
                "_latin1.write_bytes(b'{\"name\": \"caf\\xe9\"}\\n')"
            ),
            "code": "read_jsonl(str(_latin1))",
            "note": "a non-UTF-8 file raises UnicodeDecodeError naming the path; re-encode it first",
            "raises": True,
        },
        {
            "setup": "import tempfile\n_dir = tempfile.mkdtemp()",
            "code": "read_jsonl(_dir)",
            "note": "a directory is not silently treated as empty — open() raises IsADirectoryError",
            "raises": True,
        },
    ],
)
def read_jsonl(path: str, *, skip_blank: bool = True) -> list[dict]:
    """Read a JSON Lines file into a list of records, one per line.

    Use when: you have a .jsonl or .ndjson file small enough to hold in memory
        and want every record at once for filtering or aggregation.
    Don't use when: the file is larger than memory, or you only need the first
        few records — iterate the file yourself and parse line by line instead.

    Args:
        path: Filesystem path to the file. Must exist, be a regular readable
            file, and be UTF-8. Anything else raises (see Raises) — nothing is
            reported as an empty result.
        skip_blank: Ignore blank and whitespace-only lines. Set False to treat
            them as corruption and raise instead.

    Returns:
        list of the decoded objects in file order — one entry per non-skipped
        line. The annotation says list[dict] because that is the usual shape,
        but a line holding a JSON array, string, number, bool or null decodes to
        that instead and is returned as-is. A file that is empty, or (with
        skip_blank=True) holds only blank lines, gives an empty list; that is
        the only way an empty list comes back, since everything else raises.

    Raises:
        FileNotFoundError: no file at that path.
        IsADirectoryError: path is a directory. Propagated unchanged from
            open(); on Windows the same case surfaces as PermissionError.
        PermissionError: the file exists but the process may not read it.
            Propagated unchanged — never swallowed into an empty list.
        UnicodeDecodeError: the file is not valid UTF-8 (e.g. latin-1 or a
            binary file). Re-raised with the path appended to the reason. It
            carries no line number: decoding fails before the bytes can be split
            into lines. Re-encode the file, or read it yourself with the right
            encoding.
        json.JSONDecodeError: a line is not valid JSON — including a blank line
            when skip_blank=False. The message carries the line number and path,
            so use it to find the bad record rather than re-reading.

    Preconditions:
        The file must be UTF-8 encoded and readable by this process. Its
        extension is irrelevant; only the contents matter.

    Notes:
        All or nothing — the whole file is read before anything is returned, so
        a failure on any line means no records come back, not a partial list. A
        UTF-8 BOM is not stripped: it fails line 1 with json.JSONDecodeError
        ("Unexpected UTF-8 BOM"), and such a file must be read with utf-8-sig.
    """
    records: list[dict] = []
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if skip_blank and not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise json.JSONDecodeError(
                        f"{exc.msg} (line {lineno} of {path})", exc.doc, exc.pos
                    ) from None
    except UnicodeDecodeError as exc:
        # Decoding happens while iterating, so this cannot be caught per line —
        # and the offset is into the decoder's chunk, not the file. Name the
        # path instead, which is the part the caller can act on.
        raise UnicodeDecodeError(
            exc.encoding,
            exc.object,
            exc.start,
            exc.end,
            f"{exc.reason} ({path} is not valid UTF-8)",
        ) from None
    return records
