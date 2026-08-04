"""Rendering records as a markdown table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from codeact import helper


def _cell(value: Any, missing: str) -> str:
    """One record value as safe, single-line table text."""
    if value is None:
        return missing
    text = value if isinstance(value, str) else str(value)
    text = text.replace("|", "\\|")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


@helper(
    job="present",
    domains=["data", "text"],
    side_effects="none",
    examples=[
        {
            "setup": (
                "_rows = [\n"
                "    {'id': 1, 'name': 'ada'},\n"
                "    {'id': 2, 'name': 'grace', 'role': 'rear admiral'},\n"
                "]"
            ),
            "code": "print(to_markdown_table(_rows))",
            "note": "columns are the union of keys in first-seen order; 'id' and "
            "'name' come from the first record, 'role' is appended by the second",
        },
        {
            "code": "print(to_markdown_table(_rows, columns=['name', 'id']))",
            "note": "columns= both reorders and drops: 'role' is not rendered",
        },
        {
            "code": "print(to_markdown_table(_rows, missing='n/a'))",
            "note": "record 1 has no 'role' key, so that cell gets the missing marker",
        },
        {
            "code": "print(to_markdown_table([{'cmd': 'grep x | wc -l', 'note': 'two\\nlines'}]))",
            "note": "pipes are escaped and newlines become <br>, so one record "
            "always stays on one row",
        },
        {
            "code": "to_markdown_table([])",
            "note": "no records and no explicit columns means nothing to render",
        },
    ],
)
def to_markdown_table(
    records: list[dict],
    *,
    columns: Sequence[str] | None = None,
    missing: str = "",
) -> str:
    """Turn a list of record dicts into a GitHub-flavoured markdown table.

    Use when: you have already computed the rows and the reader is a human
        looking at markdown — a PR comment, an issue body, a report file, or
        terminal output where the padding keeps columns lined up.
    Don't use when: the output feeds another program (write CSV or JSON Lines
        instead), the records are nested or hold long prose (a table flattens
        both badly), or there are thousands of rows — markdown tables stop being
        readable long before that and a file the reader can sort is kinder.

    Args:
        records: The rows, each a dict of column name to value. Values are
            rendered with str(); None is treated the same as an absent key.
            Records need not share keys — see `columns`.
        columns: Column names, in the order they should appear. Names not
            present in a record render as `missing`; keys not listed are
            dropped, so this both orders and filters. Pass None (the default)
            to use every key seen, in first-seen order across the records.
        missing: Text for a cell whose key is absent or whose value is None.
            Defaults to an empty cell; "n/a" or "-" reads better in a report
            where the distinction matters.

    Returns:
        one markdown string: a header row, a `| --- |` separator row, then one
        row per record, with every cell space-padded to its column's width and
        no trailing newline. Empty string when there is nothing to render — no
        records and no explicit `columns`.

    Notes:
        Cell text is made table-safe: `|` becomes `\\|` and any newline becomes
        `<br>`, so a value containing either cannot break the row structure.
        Columns are at least three dashes wide so the separator stays valid.
    """
    if columns is None:
        cols: list[str] = []
        seen: set[str] = set()
        for record in records:
            for key in record:
                if key not in seen:
                    seen.add(key)
                    cols.append(key)
    else:
        cols = list(columns)

    if not cols:
        return ""

    head = [_cell(col, missing) for col in cols]
    body = [[_cell(record.get(col), missing) for col in cols] for record in records]
    widths = [max(3, len(text), *(len(row[i]) for row in body)) for i, text in enumerate(head)]

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    rows = [line(head), "| " + " | ".join("-" * w for w in widths) + " |"]
    rows += [line(row) for row in body]
    return "\n".join(rows)
