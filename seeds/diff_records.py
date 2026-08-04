"""Comparing two snapshots of the same keyed dataset."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from codeact import helper


def _index(records: Iterable[dict], key: str, side: str) -> dict:
    indexed: dict = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise TypeError(
                f"{side}[{position}] is {type(record).__name__}, not a dict; "
                f"{side} must be an iterable of record dicts, not a single record"
            )
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
            "code": "diff_records(iter(_before), iter(_after), 'id', ignore=['id'])",
            "note": (
                "each side is read exactly once, so one-shot iterators are fine; naming the "
                "key in ignore is a no-op — the key is never compared, so the result matches "
                "the first example"
            ),
        },
        {
            "code": "diff_records(_before, _after, 'id', ignore='owner')",
            "note": "a bare string would be iterated into letters and ignore no field at all",
            "raises": True,
        },
        {
            "code": "diff_records([{'id': 1}, {'id': 1}], [], 'id')",
            "note": "a repeated key means the field does not identify a record",
            "raises": True,
        },
    ],
)
def diff_records(
    before: Iterable[dict],
    after: Iterable[dict],
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
        before: The earlier snapshot: an iterable of flat dicts, normally a
            list. Every record must carry `key`, and no two may share the same
            value for it. Read exactly once, so a one-shot iterator (a generator,
            a csv.DictReader) is accepted; nothing is copied and nothing is
            mutated.
        after: The later snapshot, same constraints. Field names need not match
            `before`'s — fields that appear or disappear are reported.
        key: Name of the identifying field present in every record on both
            sides, e.g. "id" or "sha". Its values must be hashable. The key
            field is what the two sides are paired on, so it is never compared
            and never turns up in `changes`.
        ignore: Field names to leave out of the comparison, e.g.
            ["updated_at", "etag"] — a list, set or tuple of names, never a bare
            string (`ignore="etag"` raises rather than silently ignoring the
            letters 'e', 't', 'a', 'g'). A record differing only in ignored
            fields is reported as unchanged. Does not affect added/removed
            detection. Names that no record carries are simply never matched, so
            a typo here is silent; naming `key` is likewise accepted and does
            nothing, since the key is already excluded.

    Returns:
        dict with exactly three keys. `added` is the list of whole records from
        `after` whose key is absent from `before`, in `after` order. `removed`
        is the list of whole records from `before` whose key is absent from
        `after`, in `before` order. `changed` is a list of
        {"key": <identifying value>, "changes": {field: {"old": ..., "new": ...}}}
        — one entry per record present on both sides with at least one differing
        field, in `after` order, listing only the fields that actually differ
        and never `key` itself or an ignored field. A field present on only one
        side omits the other half, so {"new": 2} means the field appeared and
        {"old": 1} means it went away. Records equal on every compared field
        appear in none of the three lists, so three empty lists mean the
        snapshots match. The records are the caller's own dicts, referenced
        rather than copied.

    Raises:
        KeyError: a record lacks the key field. The message names the side and
            the position within it, so go fix or filter that record.
        ValueError: a key value repeats within one side. Deduplicate first, or
            pick a field that really is unique.
        TypeError: an argument is the wrong shape; the message says which, and
            for a record, where. `ignore` was given a bare string, which would
            compare against its
            individual letters and leave the field you meant diffed anyway —
            pass ignore=["updated_at"], not ignore="updated_at". Or something
            iterated out of `before`/`after` is not a dict, which is what
            passing a single record, or a dict keyed by id, amounts to. Or a key
            value is unhashable, a list say — join on a scalar field instead.

    Preconditions:
        Field values must support `==`. Comparison is by equality, so 1 and 1.0
        count as unchanged, and two equal-but-distinct objects do too. The sides
        are paired by dict lookup on the key, so key values that hash and compare
        equal — 1 and True, 2 and 2.0 — name the same record, and count as a
        duplicate when they land on the same side.
    """
    if isinstance(ignore, (str, bytes)):
        raise TypeError(
            f"ignore must be a collection of field names, not a bare "
            f"{type(ignore).__name__}: set({ignore!r}) would drop single characters "
            f"rather than a field — pass ignore=[{ignore!r}] instead"
        )
    # The key is what the sides are paired on, so comparing it can only ever
    # report a spurious change (a key value that is not equal to itself, such as
    # a nan, still matches by identity on lookup).
    ignored = set(ignore) | {key}
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
