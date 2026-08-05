"""Approving proposals from a terminal.

`codeact review` is the nicer surface, especially for trial runs. This exists
because approving a helper should not require a browser — over SSH, in a
tmux pane, or from a script, the decision still has to be reachable.
"""

from __future__ import annotations

import argparse
import sys

from .. import proposals


def register(subparsers) -> None:
    lister = subparsers.add_parser("pending", help="list proposals awaiting a decision")
    lister.add_argument("--all", action="store_true", help="include decided proposals")
    lister.set_defaults(handler=run_pending)

    shower = subparsers.add_parser("show", help="one proposal in full: card and source")
    shower.add_argument("id")
    shower.set_defaults(handler=run_show)

    approver = subparsers.add_parser("approve", help="install a proposed helper")
    approver.add_argument("id")
    approver.set_defaults(handler=run_approve)

    rejecter = subparsers.add_parser("reject", help="decline a proposal, with a reason")
    rejecter.add_argument("id")
    rejecter.add_argument("reason", nargs="?", default="")
    rejecter.set_defaults(handler=run_reject)


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
    print(
        proposal.card
        or "(none: the gate stopped before a card could be rendered — see problems above)"
    )
    print("\n--- source (the agent never sees this) ---")
    print(proposal.source or "(empty)")


def run_pending(args: argparse.Namespace) -> int:
    listing = proposals.listing("" if args.all else proposals.PENDING)
    if not listing:
        print("nothing pending")
        return 0
    for proposal in listing:
        flag = "!" if proposal.diff.get("escalates") else " "
        verb = f"revises {proposal.revises}" if proposal.revises else "new"
        print(
            f"{flag} {proposal.id}  {proposal.name:<24} {proposal.status:<9} "
            f"{verb:<20} {proposal.job}/{proposal.side_effects}"
        )
    print(f"\n{len(listing)} proposal(s). `codeact show <id>` to inspect.")
    return 0


def _load(proposal_id: str):
    proposal = proposals.load(proposal_id)
    if proposal is None:
        print(f"no proposal {proposal_id!r}", file=sys.stderr)
    return proposal


def run_show(args: argparse.Namespace) -> int:
    proposal = _load(args.id)
    if proposal is None:
        return 1
    show(proposal)
    return 0


def run_approve(args: argparse.Namespace) -> int:
    if _load(args.id) is None:
        return 1
    ok, message = proposals.approve(args.id)
    print(message)
    return 0 if ok else 1


def run_reject(args: argparse.Namespace) -> int:
    if _load(args.id) is None:
        return 1
    ok, message = proposals.reject(args.id, args.reason)
    print(message)
    return 0 if ok else 1
