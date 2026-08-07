"""`codeact mine` — what the corpus says the library is missing.

Runs out of the hot path by design: mining during a session would compete with
the task for exactly the attention the task needs. Nothing here installs into
the library: the queues are for a human to look at, and `--synthesize` goes
one step further only by drafting *proposals* — same gate, same review, same
human deciding.
"""

from __future__ import annotations

import argparse
import textwrap

from .. import miner, proposals, registry, synth


def register(subparsers) -> None:
    parser = subparsers.add_parser("mine", help="what the corpus says is missing")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="N",
        help="clusters this run may surface (default from config; 0 = uncapped)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="everything, ignoring the budget and parked clusters; counts as no showing",
    )
    parser.add_argument(
        "--synthesize",
        action="store_true",
        help="draft proposals from the queues by wrapping the claude CLI; "
        "a human still approves every one in `codeact review`",
    )
    parser.set_defaults(handler=run)


def block(code: str, width: int = 76) -> str:
    lines = code.strip().splitlines()[:12]
    out = [textwrap.shorten(line, width, placeholder=" …") for line in lines]
    return "\n".join("    " + line for line in out)


def run(args: argparse.Namespace) -> int:
    reg = registry.Registry().load()
    q = miner.queues(reg)
    if not args.all:
        # A real mine counts as a showing; `--all` is a look behind the
        # curtain and deliberately burns nothing.
        q = miner.budgeted(q, budget=args.budget, remember=True)

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

    print(f"== recurring sequences ({len(q.get('sequences') or [])}) ==")
    if not q.get("sequences"):
        print("  no repeated path through the library yet\n")
    for c in q.get("sequences") or []:
        span = f"{c['sessions']} sessions, {len(c['projects'])} projects, {c['count']} runs"
        print(f"  score {c['score']:<6} {' -> '.join(c['chain'])}   ({span})")
        print("    a recurring path is an orchestrate helper waiting for a name —")
        print("    its body is these calls, with uses=[...] declaring them")
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

    if q.get("parked"):
        print(
            f"\n{q['parked']} cluster(s) parked — shown several times without "
            "action, so they wait for new evidence. `codeact mine --all` shows "
            "everything."
        )

    if args.synthesize:
        return synthesize(reg, q)

    print("\nReview and act on these with `codeact review`.")
    return 0


def synthesize(reg, q) -> int:
    """Hand the queues to the model and gate whatever it drafts.

    The budget already shaped `q`, so synthesis costs are capped by the same
    knob that caps attention — one number to reason about, not two.
    """
    print("\n== synthesis ==")
    index = [e.card.index_line() for e in sorted(reg.available(), key=lambda e: e.name)]
    result = synth.run(q, index)
    if result.get("error"):
        print(f"  {result['error']}")
        return 1

    for p in result["filed"]:
        if p.status == proposals.PENDING:
            print(f"  PENDING  {p.name}  (proposal {p.id}) — passed the gate")
        else:
            print(f"  FAILED   {p.name}  (proposal {p.id}) — {len(p.problems)} problem(s):")
            for problem in p.problems[:4]:
                print(f"             {problem}")
    print(
        "\nNothing was installed. Approve or reject the pending drafts with "
        "`codeact review`."
    )
    return 0
