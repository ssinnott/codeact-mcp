#!/usr/bin/env python3
"""Validate helpers and capture their example output.

    python3 tools/check.py                 # check every helper
    python3 tools/check.py read_jsonl      # check one
    python3 tools/check.py --capture       # write sidecars for everything clean

This is the gate, runnable from a terminal. The review app will call the same
code paths, so what a human approves in the UI is what this reports here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import cards, contract, registry  # noqa: E402


def examples_of(entry) -> list[dict]:
    return [
        {"code": ex.code, "note": ex.note, "setup": ex.setup, "raises": ex.raises}
        for ex in entry.card.meta.examples
    ]


def check(entry, capture: bool) -> bool:
    problems = cards.validate(entry.fn)
    results = contract.run_examples(entry.path, examples_of(entry))
    failed = [r for r in results if not r["ok"]]

    ok = not problems and not failed
    print(f"{'PASS' if ok else 'FAIL'}  {entry.name}  ({entry.path.name})")
    for problem in problems:
        print(f"    card: {problem}")
    for result in failed:
        first = (result["output"] or "").strip().splitlines()
        print(f"    example: {result['code']}")
        print(f"             {first[-1] if first else '(no output)'}")

    if ok and capture:
        registry.write_sidecar(entry.path, results, helper=entry.name)
        print(f"    captured {len(results)} example output(s) -> {entry.path.stem}.json")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="helper names; default all")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="record real example output into the card's sidecar",
    )
    args = parser.parse_args()

    reg = registry.Registry().load()
    errors = reg.errors
    if args.names:
        # Only surface load failures for files the caller asked about, so
        # someone checking their own helper isn't shown someone else's mess.
        errors = [e for e in errors if any(n in e.split(":")[0] for n in args.names)]
    for error in errors:
        print(f"LOAD  {error}")

    entries = list(reg.entries.values())
    if args.names:
        wanted = set(args.names)
        missing = wanted - {e.name for e in entries}
        for name in sorted(missing):
            print(f"FAIL  {name}: no such helper")
        entries = [e for e in entries if e.name in wanted]
        if missing:
            return 1

    if not entries:
        print("no helpers found")
        return 1

    results = [check(entry, args.capture) for entry in sorted(entries, key=lambda e: e.name)]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} clean")
    return 0 if passed == len(results) and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
