"""`codeact secret` — the values approved helpers may use.

    codeact secret                       what is set, and what wants one
    codeact secret set GITHUB_TOKEN      prompts, never echoes
    codeact secret set GITHUB_TOKEN --from-env
    codeact secret remove GITHUB_TOKEN
    codeact secret where

Values are never printed back. A helper reads one only if it declared it in
`requires_secrets` and a human approved that helper.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .. import registry, secrets_store


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "secret",
        help="manage secrets an approved helper may use",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=run_list)
    sub = parser.add_subparsers(metavar="<action>")

    sub.add_parser("list", help="what is set, and what declares it").set_defaults(
        handler=run_list
    )
    sub.add_parser("where", help="where values are stored, and how").set_defaults(
        handler=run_where
    )

    setter = sub.add_parser("set", help="store a value")
    setter.add_argument("name")
    setter.add_argument(
        "--from-env", action="store_true", help="read $NAME instead of prompting"
    )
    setter.set_defaults(handler=run_set)

    remover = sub.add_parser("remove", help="delete a value")
    remover.add_argument("name")
    remover.set_defaults(handler=run_remove)


def run_where(_args: argparse.Namespace) -> int:
    print(secrets_store.describe_storage())
    return 0


def run_list(_args: argparse.Namespace) -> int:
    known = secrets_store.names()
    wanted: dict[str, list[str]] = {}
    for entry in registry.Registry().load().entries.values():
        for secret in entry.card.meta.requires_secrets:
            wanted.setdefault(secret, []).append(entry.name)

    if not known and not wanted:
        print("no secrets set, and no helper declares one")
        return 0
    for name in sorted(set(known) | set(wanted)):
        users = ", ".join(sorted(wanted.get(name, []))) or "nothing yet"
        state = "set" if name in known else "NOT SET"
        print(f"{name:<28} {state:<8} used by: {users}")
    missing = sorted(set(wanted) - set(known))
    if missing:
        print(
            f"\n{len(missing)} declared but unset — helpers needing them will fail "
            f"at the call: {', '.join(missing)}"
        )
    return 0


def run_remove(args: argparse.Namespace) -> int:
    ok = secrets_store.remove(args.name)
    print(f"removed {args.name}" if ok else f"no secret named {args.name!r}")
    return 0 if ok else 1


def run_set(args: argparse.Namespace) -> int:
    if args.from_env:
        value = os.environ.get(args.name)
        if not value:
            print(f"${args.name} is not set in the environment", file=sys.stderr)
            return 1
    else:
        value = getpass.getpass(f"value for {args.name} (not echoed): ")
    if not value:
        print("empty value, nothing stored", file=sys.stderr)
        return 1

    secrets_store.put(args.name, value)
    print(f"stored {args.name} ({len(value)} chars) in {secrets_store.store_path()}")
    print(secrets_store.describe_storage())
    return 0
