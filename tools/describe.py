#!/usr/bin/env python3
"""Print a helper's card — exactly what the agent sees, and nothing more.

    python3 tools/describe.py read_jsonl
    python3 tools/describe.py --index
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import registry, taxonomy  # noqa: E402


def main() -> int:
    reg = registry.Registry().load()
    args = sys.argv[1:]

    if not args or args[0] == "--index":
        counts = reg.counts_by_job()
        print("Jobs:")
        for job, desc in taxonomy.JOBS.items():
            print(f"  {job:<12} {counts.get(job, 0):>3}  {desc}")
        print("\nHelpers:")
        for entry in sorted(reg.available(), key=lambda e: e.name):
            print(f"  {entry.card.index_line()}")
        return 0

    entry = reg.get(args[0])
    if entry is None:
        print(f"no helper named {args[0]!r}", file=sys.stderr)
        return 1
    print(entry.card.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
