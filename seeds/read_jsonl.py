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
            "note": "blank lines are skipped by default",
        },
        {
            "code": "read_jsonl(str(_p), skip_blank=False)",
            "note": "with skip_blank=False a blank line is treated as corruption",
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
        path: Filesystem path to the file. Must exist and be UTF-8.
        skip_blank: Ignore blank and whitespace-only lines. Set False to treat
            them as corruption and raise instead.

    Returns:
        list of the decoded objects in file order — usually dicts, but whatever
        each line decodes to. An empty file gives an empty list.

    Raises:
        FileNotFoundError: no file at that path.
        json.JSONDecodeError: a line is not valid JSON; the message carries the
            line number, so use it to find the bad record rather than re-reading.

    Preconditions:
        The file must be UTF-8 encoded.
    """
    records: list[dict] = []
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
    return records
