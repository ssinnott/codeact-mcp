#!/usr/bin/env python3
"""Review proposals from a terminal.

    python3 tools/approve.py                 # list what is pending
    python3 tools/approve.py <id>            # show the full proposal
    python3 tools/approve.py <id> --approve
    python3 tools/approve.py <id> --reject "reason"

The web app (`python3 tools/review.py`) is the nicer surface, especially for
trial runs. This exists because approving a helper should not require a browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import proposals  # noqa: E402


def show(proposal) -> None:
    print(f"id       {proposal.id}")
    print(f"helper   {proposal.name}")
    print(f"status   {proposal.status}")
    if proposal.revises:
        print(f"revises  {proposal.revises}")
    print(f"job      {proposal.job} / {', '.join(proposal.domains)}")
    print(f"effects  {proposal.side_effects}")
    if proposal.requires_secrets:
        print(f"secrets  {', '.join(proposal.requires_secrets)}")
    if proposal.diff.get("escalates"):
        print("\n  !! THIS REVISION INCREASES WHAT THE HELPER CAN REACH")
        for key, change in proposal.diff.items():
            if key != "escalates":
                print(f"     {key}: {change}")
    if proposal.problems:
        print("\nproblems (blocks approval):")
        for problem in proposal.problems:
            print(f"  - {problem}")
    print("\n--- card ---")
    print(proposal.card)
    print("\n--- source (the agent never sees this) ---")
    print(proposal.source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", nargs="?")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--reject", metavar="REASON")
    parser.add_argument("--all", action="store_true", help="include decided proposals")
    args = parser.parse_args()

    if not args.id:
        pending = proposals.listing("" if args.all else proposals.PENDING)
        if not pending:
            print("nothing pending")
            return 0
        for proposal in pending:
            flag = "!" if proposal.diff.get("escalates") else " "
            verb = f"revises {proposal.revises}" if proposal.revises else "new"
            print(
                f"{flag} {proposal.id}  {proposal.name:<24} {proposal.status:<9} "
                f"{verb:<20} {proposal.job}/{proposal.side_effects}"
            )
        print(f"\n{len(pending)} proposal(s). `{sys.argv[0]} <id>` to inspect.")
        return 0

    proposal = proposals.load(args.id)
    if proposal is None:
        print(f"no proposal {args.id!r}", file=sys.stderr)
        return 1

    if args.reject:
        ok, message = proposals.reject(args.id, args.reject)
        print(message)
        return 0 if ok else 1

    if args.approve:
        ok, message = proposals.approve(args.id)
        print(message)
        return 0 if ok else 1

    show(proposal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
