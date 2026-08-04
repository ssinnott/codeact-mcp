"""Rendering records as a markdown table."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
            "note": "_rows is two records, the second carrying an extra 'role' key. "
            "Columns are the union of keys in first-seen order, so 'id' and 'name' "
            "come from the first record and 'role' is appended by the second",
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
            "code": "print(to_markdown_table(r for r in _rows if r['id'] == 2))",
            "note": "a generator renders normally: records is materialised once up "
            "front, so a one-shot iterable is not spent collecting the columns",
        },
        {
            "code": "print(to_markdown_table([{'cmd': 'grep x | wc -l', 'note': 'two\\nlines'}]))",
            "note": "pipes are escaped and newlines become <br>, so one record "
            "always stays on one row",
        },
        {
            "code": "print(to_markdown_table([{'name': '日本語', 'n': 1}, {'name': 'ada', 'n': 2}]))",
            "note": "padding counts characters, not screen columns, so the wide CJK "
            "cell overflows its padding — the markup is still valid and renders "
            "aligned, only this raw text looks ragged",
        },
        {
            "code": "to_markdown_table([])",
            "note": "no records and no columns= leaves no columns to resolve, so "
            "there is nothing to render",
        },
        {
            "code": "to_markdown_table(_rows, columns=[])",
            "note": "an empty resolved column list is the trigger, not empty input: "
            "_rows has two records and the result is still empty",
        },
        {
            "code": "to_markdown_table({'id': 1, 'name': 'ada'})",
            "note": "a single record on its own is refused rather than iterated as "
            "its keys; wrap it in a list",
            "raises": True,
        },
        {
            "code": "to_markdown_table(_rows, columns='name')",
            "note": "a bare string would silently become one column per character, "
            "so it is refused; pass ['name']",
            "raises": True,
        },
    ],
)
def to_markdown_table(
    records: Iterable[dict],
    *,
    columns: Sequence[str] | None = None,
    missing: str = "",
) -> str:
    """Turn record dicts into a GitHub-flavoured markdown table.

    Use when: you have already computed the rows and the reader is a human
        looking at markdown — a PR comment, an issue body, a report file, or
        terminal output where the padding keeps columns lined up.
    Don't use when: the output feeds another program (write CSV or JSON Lines
        instead), the records are nested or hold long prose (a table flattens
        both badly), or there are thousands of rows — markdown tables stop being
        readable long before that and a file the reader can sort is kinder.

    Args:
        records: The rows, each a dict (any Mapping) of column name to value.
            Values are rendered with str(); None is treated the same as an
            absent key. Records need not share keys — see `columns`. Any
            iterable is accepted and is consumed exactly once, so a generator
            or other one-shot iterable renders all of its rows.
        columns: Column names, in the order they should appear. Names not
            present in a record render as `missing`; keys not listed are
            dropped, so this both orders and filters. Pass None (the default)
            to use every key seen, in first-seen order across the records. Must
            be a sequence of names: a bare string is refused rather than split
            into one column per character.
        missing: Text for a cell whose key is absent or whose value is None.
            Defaults to an empty cell; "n/a" or "-" reads better in a report
            where the distinction matters.

    Returns:
        one markdown string: a header row, a `| --- |` separator row, then one
        row per record, with every cell space-padded to its column's width and
        no trailing newline. The empty string whenever the resolved column list
        comes out empty — no records, records that are all empty dicts, or
        `columns=[]`, which renders nothing even when there are records.

    Raises:
        TypeError: `records` is a single dict or string rather than an iterable
            of records, is not iterable at all, holds an element that is not a
            dict, or `columns` is a bare string. Each of these would otherwise
            produce a table that looks fine and says nothing, so it is refused
            instead.

    Notes:
        Cell text is made table-safe: `|` becomes `\\|` and any newline becomes
        `<br>`, so a value containing either cannot break the row structure.
        Columns are at least three dashes wide so the separator stays valid.
        Column widths are measured with len(), which counts code points rather
        than display columns, so CJK, emoji and combining marks pad short or
        long and the raw text looks ragged. The markup stays valid and any
        markdown renderer aligns the columns regardless; only reading the
        unrendered source suffers. Aligning those too would need a full
        east-asian-width and grapheme walk, which this helper does not do.
    """
    if isinstance(records, (Mapping, str, bytes)):
        raise TypeError(
            f"records must be an iterable of record dicts, not a single "
            f"{type(records).__name__}; wrap one record in a list"
        )
    if isinstance(columns, (str, bytes)):
        raise TypeError(
            f"columns must be a sequence of column names, not {columns!r}; "
            f"pass a list such as ['{columns}']"
        )

    rows = list(records)
    for index, record in enumerate(rows):
        if not isinstance(record, Mapping):
            raise TypeError(
                f"records[{index}] is a {type(record).__name__}, not a dict; "
                f"every record must map column name to value"
            )

    if columns is None:
        cols: list[str] = []
        seen: set[str] = set()
        for record in rows:
            for key in record:
                if key not in seen:
                    seen.add(key)
                    cols.append(key)
    else:
        cols = list(columns)

    if not cols:
        return ""

    head = [_cell(col, missing) for col in cols]
    body = [[_cell(record.get(col), missing) for col in cols] for record in rows]
    widths = [max(3, len(text), *(len(row[i]) for row in body)) for i, text in enumerate(head)]

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    lines = [line(head), "| " + " | ".join("-" * w for w in widths) + " |"]
    lines += [line(row) for row in body]
    return "\n".join(lines)
