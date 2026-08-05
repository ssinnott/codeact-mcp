"""One command for everything a human does to the library.

There is exactly one thing a person operates here — a helper library with a
policy attached — so there is one command for it, not nine scripts that each
have to be remembered by filename. Every surface hangs off `codeact`:

    codeact                     what the library and its policies look like now
    codeact review              approve or reject proposals in a browser
    codeact policy enforce      route kubectl and friends through CodeAct

The agent's surface is the MCP server; this is yours. Nothing here is callable
from a session, which is the point — approving a helper, holding a secret, and
deciding what may reach a real cluster are all decisions the agent must not be
able to make for itself.

Each subcommand lives in its own module with a `register(subparsers)` that
attaches its parser and sets `handler`. Adding one means writing that function
and adding the module to `GROUPS` below.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import approvals, evals, library, mining, overview, policy, review, sandbox, secret

# Order is the order they appear in --help, grouped by what you'd be doing.
MODULES = (overview, review, approvals, library, mining, policy, secret, sandbox, evals)

EPILOG = """\
where to start:
  codeact                      everything's current state, on one screen
  codeact review               decide on what the agent has proposed
  codeact check --capture      validate helpers and record their real output
  codeact policy enforce       route kubectl and friends through CodeAct

`codeact <command> --help` for the detail on any of them. The library lives in
~/.codeact, one per machine, and every command here reads or writes it.
"""


def root() -> Path:
    """The plugin directory — where `codeact`, `evals/` and `seeds/` live."""
    return Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeact",
        description="The human side of the CodeAct helper library.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # The subcommand list is suppressed in favour of the grouped epilog above:
    # argparse's own listing is a single unreadable run of nineteen names.
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in MODULES:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        # Bare `codeact` reports rather than complaining, since "what is the
        # state of this thing" is the most common reason to run it at all.
        handler = overview.run
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `codeact card --index | head` is a normal thing to do, and it closes
        # the pipe mid-write. Point stdout at /dev/null so the interpreter's
        # own flush at exit doesn't turn that into a second error message.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
