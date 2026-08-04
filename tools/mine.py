#!/usr/bin/env python3
"""Mine the corpus for helper candidates and library problems.

    python3 tools/mine.py

Runs out of the hot path by design — mining during a session would compete with
the task for exactly the attention the task needs. Nothing here changes the
library; it produces queues for a human to look at.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import miner, registry  # noqa: E402


def block(code: str, width: int = 76) -> str:
    lines = code.strip().splitlines()[:12]
    out = [textwrap.shorten(line, width, placeholder=" …") for line in lines]
    return "\n".join("    " + line for line in out)


def main() -> int:
    reg = registry.Registry().load()
    q = miner.queues(reg)

    print(f"corpus: {q['corpus_size']} recorded blocks, {len(reg.entries)} helpers\n")
    if not q["corpus_size"]:
        print("Nothing recorded yet. The corpus fills as run_python is used, and")
        print("patterns need to repeat across sessions before they mean anything.")
        return 0

    print(f"== helper candidates ({len(q['candidates'])}) ==")
    if not q["candidates"]:
        print("  nothing recurring across sessions yet\n")
    for c in q["candidates"]:
        span = f"{c['sessions']} sessions, {len(c['projects'])} projects, {c['count']} runs"
        fails = f", {c['failures']} needed fixing first" if c["failures"] else ""
        print(f"  score {c['score']:<6} {span}{fails}")
        print(block(c["code"]))
        print()

    print(f"== retrieval failures ({len(q['retrieval_failures'])}) ==")
    if not q["retrieval_failures"]:
        print("  none — no helper was re-implemented inline\n")
    for c in q["retrieval_failures"]:
        print(f"  {c['helper']} rewritten by hand {c['count']}×")
        print("    the library was fine and DISCOVERY failed — fix the card or its tags")
        print(block(c["code"]))
        print()

    print(f"== needs revision ({len(q['revisions'])}) ==")
    for r in q["revisions"]:
        if r.get("quarantined"):
            print(f"  {r['name']}: QUARANTINED — {r['quarantined']}")
        else:
            print(f"  {r['name']}: {r['failures']}/{r['calls']} calls failed ({r['rate']:.0%})")
    if not q["revisions"]:
        print("  none")

    print(f"\n== never used ({len(q['removals'])}) ==")
    print("  " + (", ".join(q["removals"]) if q["removals"] else "none"))
    print("\nReview and act on these with `python3 tools/review.py`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
