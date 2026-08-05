"""`codeact policy` — which shell commands route through CodeAct, not Bash.

    codeact policy                       what is governed, and how
    codeact policy enforce               off | audit | ask | enforce
    codeact policy block terraform       add a rule
    codeact policy unblock terraform
    codeact policy check 'kubectl get pods'
    codeact policy log

`check` is the one to reach for while tuning: it prints the decision, the
target it resolved, and the refusal the agent would read — without running
anything.
"""

from __future__ import annotations

import argparse
import json

from .. import commands, config, paths


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "policy",
        help="which shell commands route through CodeAct",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=run_status)
    sub = parser.add_subparsers(metavar="<action>")

    for mode in commands.MODES:
        setter = sub.add_parser(mode, help=f"set the mode to {mode}")
        setter.set_defaults(handler=run_mode, mode=mode)

    blocker = sub.add_parser("block", help="route a command through CodeAct")
    blocker.add_argument("command")
    blocker.add_argument(
        "--family",
        choices=["kube"],
        help="reuse a family's target resolution and read-only verbs",
    )
    blocker.set_defaults(handler=run_block)

    unblocker = sub.add_parser("unblock", help="stop governing a command")
    unblocker.add_argument("command")
    unblocker.set_defaults(handler=run_unblock)

    checker = sub.add_parser("check", help="decide a command line without running it")
    checker.add_argument("line")
    checker.add_argument(
        "--codeact",
        action="store_true",
        help="decide as the interpreter would, rather than as Bash",
    )
    checker.set_defaults(handler=run_check)

    logs = sub.add_parser("log", help="what the policy has seen")
    logs.add_argument("-n", type=int, default=20)
    logs.set_defaults(handler=run_log)


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


def run_status(_args: argparse.Namespace) -> int:
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


def run_check(args: argparse.Namespace) -> int:
    policy = commands.Policy.load(config.load())
    if not policy.active:
        print(f"[mode is {policy.mode} — this is what it would decide if turned on]\n")
    surface = commands.CODEACT if args.codeact else commands.BASH
    decision = policy.check_line(
        args.line,
        surface=surface,
        resolve=None if args.codeact else commands.resolve_kube_context,
    )
    verdict = "ALLOW" if decision.allowed else "DENY"
    where = "run_python" if args.codeact else "Bash"
    print(f"{verdict}  ({where})  {decision.reason or 'no rule matches'}")
    if decision.target:
        print(f"target: {decision.target}")
    if decision.message:
        print()
        print(decision.message)
    return 0 if decision.allowed else 1


def run_mode(args: argparse.Namespace) -> int:
    settings = config.load()
    _rules(settings)
    settings["commands"]["mode"] = args.mode
    config.save(settings)
    print(f"mode: {args.mode}")
    if args.mode != "off":
        print("Restart Claude Code so the interpreter picks it up.")
    return 0


def run_block(args: argparse.Namespace) -> int:
    settings = config.load()
    rules = _rules(settings)
    if any(_name_of(r) == args.command for r in rules):
        print(f"{args.command} is already governed")
        return 0
    rules.append(
        args.command if args.family is None else {"command": args.command, "family": args.family}
    )
    config.save(settings)
    print(f"blocked in Bash: {args.command}")
    if settings["commands"]["mode"] == "off":
        print("Mode is still `off` — `codeact policy enforce` turns it on.")
    return 0


def run_unblock(args: argparse.Namespace) -> int:
    settings = config.load()
    rules = _rules(settings)
    kept = [r for r in rules if _name_of(r) != args.command]
    if len(kept) == len(rules):
        print(f"{args.command} was not governed")
        return 0
    settings["commands"]["rules"] = kept
    config.save(settings)
    print(f"no longer governed: {args.command}")
    return 0


def run_log(args: argparse.Namespace) -> int:
    path = paths.root() / "commands.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"nothing recorded yet ({path})")
        return 0
    for line in lines[-args.n :]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        mark = "allow" if entry.get("allowed") else "DENY "
        print(
            f"{mark} {entry.get('surface'):8} {entry.get('reason', ''):46} "
            f"{entry.get('line', '')}"
        )
    return 0
