#!/usr/bin/env python3
"""Which shell commands go through CodeAct instead of Bash.

    python3 tools/policy.py status
    python3 tools/policy.py mode enforce         # off | audit | ask | enforce
    python3 tools/policy.py block terraform      # add a rule
    python3 tools/policy.py unblock terraform
    python3 tools/policy.py check 'kubectl get pods'
    python3 tools/policy.py check --codeact 'kubectl delete ns//x'
    python3 tools/policy.py log                  # what the policy has seen

`check` is the one to reach for while tuning: it prints the decision, the
target it resolved, and the refusal the agent would read — without running
anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import commands, config, paths  # noqa: E402


def _rules(settings: dict) -> list:
    block = settings.get("commands")
    if not isinstance(block, dict):
        block = dict(config.DEFAULTS["commands"])
        settings["commands"] = block
    block.setdefault("mode", "off")
    block.setdefault("rules", list(config.DEFAULTS["commands"]["rules"]))
    return block["rules"]


def _name_of(spec) -> str:
    return spec if isinstance(spec, str) else str(spec.get("command", ""))


def status() -> int:
    policy = commands.Policy.load(config.load())
    print(f"mode: {policy.mode}" + ("" if policy.active else "  (nothing is checked)"))
    if not policy.rules:
        print("no rules")
        return 0
    print()
    for rule in policy.rules.values():
        if rule.classifies_reads:
            print(f"  {rule.command}: Bash only against {', '.join(rule.local[:4])}, …")
            reads = " ".join(sorted(rule.read_only))
            print(
                f"      read-only in CodeAct: {reads}"
                if reads
                else "      read-only in CodeAct: none declared — every verb counts "
                "as a write. Add `read_only` to the rule."
            )
            if rule.always_allow:
                print(f"      always allowed: {' '.join(sorted(rule.always_allow))}")
        else:
            print(f"  {rule.command}: blocked in Bash; CodeAct left to helper review")
    print()
    print(f"config: {paths.root() / 'config.json'}")
    return 0


def check(line: str, codeact: bool) -> int:
    policy = commands.Policy.load(config.load())
    if not policy.active:
        print(f"[mode is {policy.mode} — this is what it would decide if turned on]\n")
    surface = commands.CODEACT if codeact else commands.BASH
    decision = policy.check_line(
        line,
        surface=surface,
        resolve=None if codeact else commands.resolve_kube_context,
    )
    verdict = "ALLOW" if decision.allowed else "DENY"
    where = "run_python" if codeact else "Bash"
    print(f"{verdict}  ({where})  {decision.reason or 'no rule matches'}")
    if decision.target:
        print(f"target: {decision.target}")
    if decision.message:
        print()
        print(decision.message)
    return 0 if decision.allowed else 1


def set_mode(mode: str) -> int:
    settings = config.load()
    _rules(settings)
    settings["commands"]["mode"] = mode
    config.save(settings)
    print(f"mode: {mode}")
    if mode != "off":
        print("Restart Claude Code so the interpreter picks it up.")
    return 0


def block(name: str, family: str | None) -> int:
    settings = config.load()
    rules = _rules(settings)
    if any(_name_of(r) == name for r in rules):
        print(f"{name} is already governed")
        return 0
    rules.append(name if family is None else {"command": name, "family": family})
    config.save(settings)
    print(f"blocked in Bash: {name}")
    if settings["commands"]["mode"] == "off":
        print("Mode is still `off` — `tools/policy.py mode enforce` turns it on.")
    return 0


def unblock(name: str) -> int:
    settings = config.load()
    rules = _rules(settings)
    kept = [r for r in rules if _name_of(r) != name]
    if len(kept) == len(rules):
        print(f"{name} was not governed")
        return 0
    settings["commands"]["rules"] = kept
    config.save(settings)
    print(f"no longer governed: {name}")
    return 0


def show_log(limit: int) -> int:
    path = paths.root() / "commands.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"nothing recorded yet ({path})")
        return 0
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        mark = "allow" if entry.get("allowed") else "DENY "
        print(f"{mark} {entry.get('surface'):8} {entry.get('reason', ''):46} {entry.get('line', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status")
    mode = sub.add_parser("mode")
    mode.add_argument("mode", choices=commands.MODES)
    blocker = sub.add_parser("block")
    blocker.add_argument("command")
    blocker.add_argument(
        "--family",
        choices=["kube"],
        help="reuse a family's target resolution and read-only verbs",
    )
    unblocker = sub.add_parser("unblock")
    unblocker.add_argument("command")
    checker = sub.add_parser("check")
    checker.add_argument("line")
    checker.add_argument(
        "--codeact",
        action="store_true",
        help="decide as the interpreter would, rather than as Bash",
    )
    logs = sub.add_parser("log")
    logs.add_argument("-n", type=int, default=20)
    args = parser.parse_args()

    if args.cmd == "mode":
        return set_mode(args.mode)
    if args.cmd == "block":
        return block(args.command, args.family)
    if args.cmd == "unblock":
        return unblock(args.command)
    if args.cmd == "check":
        return check(args.line, args.codeact)
    if args.cmd == "log":
        return show_log(args.n)
    return status()


if __name__ == "__main__":
    sys.exit(main())
