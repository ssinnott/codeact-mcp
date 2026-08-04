"""Bucketing records by a shared field or computed key."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codeact import helper


@helper(
    job="transform",
    domains=["data"],
    examples=[
        {
            "setup": (
                "_rows = [\n"
                "    {'id': 1, 'team': 'core'},\n"
                "    {'id': 2, 'team': 'infra'},\n"
                "    {'id': 3, 'team': 'core'},\n"
                "    {'id': 4},\n"
                "]"
            ),
            "code": "group_by(_rows, 'team')",
            "note": "id 4 has no 'team' field, so it is dropped by default",
        },
        {
            "code": "group_by(_rows, lambda r: r.get('team', 'unassigned'))",
            "note": "a callable key is how you give the field-less records a bucket",
        },
        {
            "code": "group_by(_rows, 'team', on_missing='raise')",
            "note": "on_missing='raise' turns a missing field into a KeyError naming the index",
            "raises": True,
        },
    ],
)
def group_by(
    records: list[dict],
    key: str | Callable[[dict], Any],
    *,
    on_missing: str = "skip",
) -> dict[Any, list[dict]]:
    """Split a flat list of records into buckets that share a key value.

    Use when: you have records from a query, CSV or JSONL and need them
        organised per category before counting, summarising or reporting —
        e.g. commits per author, rows per status, files per extension.
    Don't use when: you only need one bucket (filter with a list comprehension),
        you want a count per key rather than the records themselves
        (collections.Counter is smaller), or the records are already sorted by
        the key and you want to stream them (itertools.groupby avoids holding
        them all in memory).

    Args:
        records: The records to split. Each is a dict; they are referenced, not
            copied, so the lists in the result contain the very same objects.
        key: Either a field name to read from each record, or a callable taking
            one record and returning its group key. The resulting key values
            must be hashable — strings, numbers, tuples, None.
        on_missing: What to do with a record that lacks the named field, when
            `key` is a field name. "skip" (default) leaves the record out of the
            result entirely; "raise" raises KeyError instead. Ignored when `key`
            is a callable — a callable decides for itself, which is the usual
            way to bucket such records (`lambda r: r.get("team", "unknown")`).

    Returns:
        dict mapping each distinct key value to the list of records carrying it.
        Keys appear in the order they were first seen and records stay in input
        order within each list. An empty input, or an input where every record
        was skipped, gives an empty dict. Records are never duplicated across
        buckets.

    Raises:
        KeyError: a record lacks the named field and on_missing="raise". The
            message carries the record's 0-based index in `records`, so use it
            to find the offending row rather than re-scanning.
        ValueError: on_missing is neither "skip" nor "raise".
        TypeError: a key value is unhashable (a list or dict, typically) — reach
            for a callable that converts it, e.g. `lambda r: tuple(r["tags"])`.
    """
    if on_missing not in ("skip", "raise"):
        raise ValueError(f"on_missing must be 'skip' or 'raise', got {on_missing!r}")

    key_fn = key if callable(key) else None
    groups: dict[Any, list[dict]] = {}
    for index, record in enumerate(records):
        if key_fn is not None:
            value = key_fn(record)
        else:
            if key not in record:
                if on_missing == "raise":
                    raise KeyError(f"record {index} has no field {key!r}")
                continue
            value = record[key]
        groups.setdefault(value, []).append(record)
    return groups
