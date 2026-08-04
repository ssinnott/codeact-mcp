#!/usr/bin/env python3
"""Manage secrets the helper library may use.

    python3 tools/secret.py list
    python3 tools/secret.py set GITHUB_TOKEN        # prompts, never echoes
    python3 tools/secret.py set GITHUB_TOKEN --from-env
    python3 tools/secret.py remove GITHUB_TOKEN
    python3 tools/secret.py where

Values are never printed back. A helper reads one only if it declared it in
`requires_secrets` and a human approved that helper.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import registry, secrets_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("where")
    setter = sub.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("--from-env", action="store_true", help="read $NAME instead of prompting")
    remover = sub.add_parser("remove")
    remover.add_argument("name")
    args = parser.parse_args()

    if args.cmd == "where":
        print(secrets_store.describe_storage())
        return 0

    if args.cmd == "list":
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

    if args.cmd == "remove":
        ok = secrets_store.remove(args.name)
        print(f"removed {args.name}" if ok else f"no secret named {args.name!r}")
        return 0 if ok else 1

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


if __name__ == "__main__":
    raise SystemExit(main())
