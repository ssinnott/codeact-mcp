"""Comparing two snapshots of the same keyed dataset."""

from __future__ import annotations

from collections.abc import Collection

from codeact import helper


def _index(records: list[dict], key: str, side: str) -> dict:
    indexed: dict = {}
    for position, record in enumerate(records):
        if key not in record:
            raise KeyError(f"{side}[{position}] has no field {key!r}")
        value = record[key]
        if value in indexed:
            raise ValueError(f"duplicate {key}={value!r} in {side}; the key must identify one record")
        indexed[value] = record
    return indexed


@helper(
    job="transform",
    domains=["data"],
    examples=[
        {
            "setup": (
                "_before = [\n"
                "    {'id': 1, 'status': 'open', 'owner': 'ana'},\n"
                "    {'id': 2, 'status': 'open', 'owner': 'bo'},\n"
                "    {'id': 3, 'status': 'done', 'owner': 'cy'},\n"
                "]\n"
                "_after = [\n"
                "    {'id': 1, 'status': 'open', 'owner': 'ana'},\n"
                "    {'id': 2, 'status': 'closed', 'owner': 'ana'},\n"
                "    {'id': 4, 'status': 'new', 'owner': 'di'},\n"
                "]"
            ),
            "code": "diff_records(_before, _after, 'id')",
            "note": "id 1 is unchanged so it appears nowhere; id 2 lists both differing fields",
        },
        {
            "code": "diff_records(_before, _after, 'id', ignore=['owner'])",
            "note": "ignore drops a field from the comparison — here only 'status' can differ",
        },
        {
            "code": "diff_records([{'id': 1, 'a': 1}], [{'id': 1, 'b': 2}], 'id')",
            "note": "a field on only one side reports just 'old' (dropped) or just 'new' (added)",
        },
        {
            "code": "diff_records([{'id': 1}, {'id': 1}], [], 'id')",
            "note": "a repeated key means the field does not identify a record",
            "raises": True,
        },
    ],
)
def diff_records(
    before: list[dict],
    after: list[dict],
    key: str,
    *,
    ignore: Collection[str] = (),
) -> dict[str, list]:
    """Report what changed between two snapshots of the same keyed dataset.

    Use when: you have the same collection at two points in time — an API pulled
        twice, yesterday's and today's export, a table before and after a
        migration — and need to say precisely which records appeared, vanished,
        or had fields edited.
    Don't use when: the two lists have no stable identifier to join on (sort and
        compare, or diff the serialized text), you only need "are these equal"
        (compare the lists directly), or you are comparing nested/tree-shaped
        documents — this compares each record's fields one level deep only.

    Args:
        before: The earlier snapshot, a list of flat dicts. Every record must
            carry `key`, and no two may share the same value for it.
        after: The later snapshot, same constraints. Field names need not match
            `before`'s — fields that appear or disappear are reported.
        key: Name of the identifying field present in every record on both
            sides, e.g. "id" or "sha". Its values must be hashable.
        ignore: Field names to leave out of the comparison, e.g.
            ["updated_at", "etag"]. A record differing only in ignored fields is
            reported as unchanged. Does not affect added/removed detection.

    Returns:
        dict with exactly three keys. `added` is the list of whole records from
        `after` whose key is absent from `before`, in `after` order. `removed`
        is the list of whole records from `before` whose key is absent from
        `after`, in `before` order. `changed` is a list of
        {"key": <identifying value>, "changes": {field: {"old": ..., "new": ...}}}
        — one entry per record present on both sides with at least one differing
        field, in `after` order, listing only the fields that actually differ. A
        field present on only one side omits the other half, so {"new": 2} means
        the field appeared and {"old": 1} means it went away. Records equal on
        every compared field appear in none of the three lists, so three empty
        lists mean the snapshots match. The records are the caller's own dicts,
        referenced rather than copied.

    Raises:
        KeyError: a record lacks the key field. The message names the side and
            the position within it, so go fix or filter that record.
        ValueError: a key value repeats within one side. Deduplicate first, or
            pick a field that really is unique.
        TypeError: a key value is unhashable (a list, say) — join on a scalar
            field instead.

    Preconditions:
        Field values must support `==`. Comparison is by equality, so 1 and 1.0
        count as unchanged, and two equal-but-distinct objects do too.
    """
    ignored = set(ignore)
    old_by_key = _index(before, key, "before")
    new_by_key = _index(after, key, "after")

    added = [record for value, record in new_by_key.items() if value not in old_by_key]
    removed = [record for value, record in old_by_key.items() if value not in new_by_key]

    changed: list[dict] = []
    for value, new_record in new_by_key.items():
        old_record = old_by_key.get(value)
        if old_record is None:
            continue
        fields = list(old_record) + [f for f in new_record if f not in old_record]
        changes: dict = {}
        for field in fields:
            if field in ignored:
                continue
            in_old, in_new = field in old_record, field in new_record
            if in_old and in_new:
                if old_record[field] != new_record[field]:
                    changes[field] = {"old": old_record[field], "new": new_record[field]}
            elif in_old:
                changes[field] = {"old": old_record[field]}
            else:
                changes[field] = {"new": new_record[field]}
        if changes:
            changed.append({"key": value, "changes": changes})

    return {"added": added, "removed": removed, "changed": changed}
