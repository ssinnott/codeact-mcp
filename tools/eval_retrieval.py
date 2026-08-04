#!/usr/bin/env python3
"""Score task-driven discovery against a gold set.

The eval deliberately splits the two halves that can each fail on their own:
the model classifies a task into jobs and domains, and the server ranks. Feeding
recorded classifications through the real ranker isolates which half is at fault
when a lookup misses.

    python3 tools/eval_retrieval.py evals/retrieval.json

Input is a JSON list of {task, gold, jobs, domains, query}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import registry, search  # noqa: E402


def rank_of(pool, name: str, **kwargs) -> int | None:
    found = search.search(pool, limit=50, **kwargs)
    for position, entry in enumerate(found, start=1):
        if entry.name == name:
            return position
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    cases = json.loads(Path(sys.argv[1]).read_text())
    pool = registry.Registry().load().available()
    if not pool:
        print("no helpers loaded")
        return 1

    hit1 = hit3 = found = 0
    reciprocal = 0.0
    rows = []

    for case in cases:
        gold = case["gold"]
        classified = rank_of(
            pool,
            gold,
            task=case.get("query", case["task"]),
            jobs=case.get("jobs") or (),
            domains=case.get("domains") or (),
        )
        # The same lookup without the model's category filter, to show whether
        # classification helped or hurt.
        unfiltered = rank_of(pool, gold, task=case.get("query", case["task"]))

        if classified:
            found += 1
            reciprocal += 1 / classified
            hit1 += classified == 1
            hit3 += classified <= 3

        rows.append((case["task"], gold, classified, unfiltered))

    total = len(cases)
    print(f"{'rank':>5} {'nofilter':>9}  gold / task")
    print("-" * 78)
    for task, gold, classified, unfiltered in rows:
        mark = "  " if classified == 1 else ("~ " if classified and classified <= 3 else "X ")
        print(f"{mark}{str(classified or '-'):>3} {str(unfiltered or '-'):>9}  {gold} / {task[:44]}")

    print("-" * 78)
    print(f"hit@1  {hit1}/{total}  ({hit1 / total:.0%})")
    print(f"hit@3  {hit3}/{total}  ({hit3 / total:.0%})")
    print(f"found  {found}/{total}")
    print(f"MRR    {reciprocal / total:.3f}")

    misfiltered = [r for r in rows if r[2] is None and r[3] is not None]
    if misfiltered:
        print(
            f"\n{len(misfiltered)} case(s) where the model's job/domain filter EXCLUDED the "
            "right helper that plain ranking would have found:"
        )
        for task, gold, _, unfiltered in misfiltered:
            print(f"  {gold} (rank {unfiltered} unfiltered) — {task[:60]}")

    return 0 if hit3 == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
