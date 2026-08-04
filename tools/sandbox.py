#!/usr/bin/env python3
"""Check — and explain — running the interpreter as a separate OS user.

    python3 tools/sandbox.py                  # report on the current setting
    python3 tools/sandbox.py codeact-runner   # check that user specifically

This is the only *enforced* boundary the system has. The AST guard is a policy
layer that Python's dynamism can defeat; a different uid is enforced by the
kernel, and it is what actually stops the interpreter reading your ssh keys.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import config, interpreter, paths, secrets_store  # noqa: E402

OK, BAD, INFO = "  ok  ", " FAIL ", "  --  "


def check(runner: str) -> bool:
    print(f"target user   {runner}")
    print(f"running as    {pwd.getpwuid(os.getuid()).pw_name} (uid {os.getuid()})\n")

    try:
        record = pwd.getpwnam(runner)
    except KeyError:
        print(f"{BAD} no such user {runner!r}")
        print(f"       create one with:  sudo useradd --system --no-create-home {runner}")
        return False
    print(f"{OK} user exists (uid {record.pw_uid})")

    if os.getuid() == 0:
        print(f"{INFO} running as root, so the worker can drop directly — no sudo needed")
        print(f"{INFO} note that running Claude Code as root is itself worth avoiding")
    else:
        probe = subprocess.run(
            ["sudo", "-n", "-u", runner, sys.executable, "-c", "import os;print(os.getuid())"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            print(f"{BAD} sudo cannot run the interpreter as {runner}")
            print(f"       {(probe.stderr or '').strip().splitlines()[-1:] or ['']}"[:200])
            print("\n       Add this to /etc/sudoers.d/codeact (chmod 440):\n")
            print(f"         {interpreter.sudoers_line(runner)}\n")
            return False
        print(f"{OK} sudo works without a password (child ran as uid {probe.stdout.strip()})")

    # What the runner can and cannot reach is the whole point, so report it.
    print()
    _reachability(runner, Path.cwd(), "your project")
    _reachability(runner, paths.helpers_dir(), "the helper library")
    _reachability(runner, secrets_store.store_path(), "the secret store", want_denied=True)
    _reachability(runner, Path.home() / ".ssh", "your ~/.ssh", want_denied=True)
    return True


def _readable_as(runner: str, target: Path) -> bool | None:
    if not target.exists():
        return None
    if os.getuid() == 0:
        args = ["setpriv", "--reuid", runner, "--regid", "nogroup", "--clear-groups"]
        probe = subprocess.run(
            [*args, "test", "-r", str(target)], capture_output=True
        )
        if probe.returncode not in (0, 1):
            return None
        return probe.returncode == 0
    probe = subprocess.run(
        ["sudo", "-n", "-u", runner, "test", "-r", str(target)], capture_output=True
    )
    if probe.returncode not in (0, 1):
        return None
    return probe.returncode == 0


def _reachability(runner: str, target: Path, label: str, want_denied: bool = False) -> None:
    readable = _readable_as(runner, target)
    if readable is None:
        print(f"{INFO} {label}: does not exist, or could not be checked ({target})")
        return
    if want_denied:
        mark = OK if not readable else BAD
        state = "unreadable — good" if not readable else "READABLE — the isolation is not buying you much"
    else:
        mark = OK if readable else BAD
        state = "readable" if readable else "NOT readable, so the agent cannot work here"
    print(f"{mark} {label}: {state}\n       {target}")


def main() -> int:
    runner = sys.argv[1] if len(sys.argv) > 1 else config.get("run_as")
    if not runner:
        print("run_as is not set — the interpreter runs as you, the same as Bash does.\n")
        print("To isolate it, create a user and point config at it:\n")
        print("  sudo useradd --system --no-create-home codeact-runner")
        print('  # ~/.codeact/config.json:  {"run_as": "codeact-runner"}\n')
        print("Then re-run this to check the setup. Note the trade-off: a separate")
        print("user cannot write to your project, which is usually what you want from")
        print("run_python but will break helpers that legitimately write files.")
        return 0

    ok = check(runner)
    print()
    if ok:
        print("Ready. Set this in ~/.codeact/config.json:")
        print(f'  {{"run_as": "{runner}"}}')
        print("\nIf a helper needs a secret, note that the secret store is 0600 and owned")
        print("by you — the runner cannot read it. That is the correct default, and it")
        print("means secret-using helpers need the broker described in DESIGN.md §10.")
    else:
        print("Not ready. The interpreter will refuse to start rather than quietly")
        print("running with your full privileges.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
