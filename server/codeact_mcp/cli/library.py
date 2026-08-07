"""Inspecting and validating the library itself.

`check` is the gate, runnable from a terminal: the review app calls the same
code paths, so what a human approves in the UI is what this reports here.
`card` prints exactly what the agent sees and nothing more — which is the only
way to judge a card, since the agent never gets the source to fall back on.
"""

from __future__ import annotations

import argparse
import sys

from .. import cards, contract, linkage, registry, taxonomy


def register(subparsers) -> None:
    checker = subparsers.add_parser("check", help="validate helpers and their examples")
    checker.add_argument("names", nargs="*", help="helper names; default all")
    checker.add_argument(
        "--capture",
        action="store_true",
        help="record real example output into the card's sidecar",
    )
    checker.set_defaults(handler=run_check)

    carder = subparsers.add_parser("card", help="print a card as the agent sees it")
    carder.add_argument("name", nargs="?")
    carder.add_argument(
        "--index", action="store_true", help="the job index and the whole catalog"
    )
    carder.set_defaults(handler=run_card)


def examples_of(entry) -> list[dict]:
    return [
        {"code": ex.code, "note": ex.note, "setup": ex.setup, "raises": ex.raises}
        for ex in entry.card.meta.examples
    ]


def check(entry, capture: bool) -> bool:
    problems = cards.validate(entry.fn, reach=entry.card.reach)
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


def run_check(args: argparse.Namespace) -> int:
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
            reason = reg.broken_reason(name)
            print(f"FAIL  {name}: " + (f"failed to load: {reason}" if reason else "no such helper"))
        entries = [e for e in entries if e.name in wanted]
        if missing:
            return 1

    if not entries:
        print("no helpers found")
        return 1

    results = [check(entry, args.capture) for entry in sorted(entries, key=lambda e: e.name)]
    passed = sum(results)

    core_clean = True
    if not args.names:
        core_clean = check_core(reg)

    print(f"\n{passed}/{len(results)} clean")
    return 0 if passed == len(results) and core_clean and not errors else 1


def check_core(reg) -> bool:
    """Validate the core layer: purity, and whether anything covers it.

    Core has no examples because it has no card, so it is verified through the
    helpers that use it (§12) — which makes a core module with no dependents
    genuinely unverified, and worth saying so out loud.
    """
    names = linkage.core_names()
    if not names:
        return True

    dependents: dict[str, list[str]] = {name: [] for name in names}
    for entry in reg.entries.values():
        for closure_name, _ in linkage.closure(entry.path):
            layer, _, stem = closure_name.partition("/")
            if layer == "core" and stem in dependents:
                dependents[stem].append(entry.name)

    clean = True
    print()
    for name in names:
        path = linkage.resolve("core", name)
        problems = linkage.core_problems(path) if path else [f"core/{name}: vanished"]
        ok = not problems
        clean = clean and ok
        users = ", ".join(sorted(dependents[name]))
        coverage = f"used by: {users}" if users else "NO DEPENDENTS — nothing verifies it"
        print(f"{'PASS' if ok else 'FAIL'}  core/{name}  ({coverage})")
        for problem in problems:
            print(f"    {problem}")
    return clean


def run_card(args: argparse.Namespace) -> int:
    reg = registry.Registry().load()

    if args.index or not args.name:
        counts = reg.counts_by_job()
        print("Jobs:")
        for job, desc in taxonomy.JOBS.items():
            print(f"  {job:<12} {counts.get(job, 0):>3}  {desc}")
        print("\nHelpers:")
        for entry in sorted(reg.available(), key=lambda e: e.name):
            print(f"  {entry.card.index_line()}")
        broken = [name for name in sorted(reg.broken) if reg.broken_reason(name)]
        if broken:
            print("\nBroken (on disk, not callable — `codeact check` for the reason):")
            for name in broken:
                print(f"  {name} — {reg.broken_reason(name)}")
        return 0

    entry = reg.get(args.name)
    if entry is None:
        reason = reg.broken_reason(args.name)
        detail = f"{args.name} failed to load: {reason}" if reason else (
            f"no helper named {args.name!r}"
        )
        print(detail, file=sys.stderr)
        return 1
    print(entry.card.render())
    return 0
