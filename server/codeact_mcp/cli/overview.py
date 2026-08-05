"""`codeact status` — the whole system on one screen.

Six things decide how this behaves and they live in five different places: the
helper library, the pending queue, the guard, the command policy, and whether
the interpreter runs as someone else. Anyone coming back to a machine after a
month needs all six before they can safely do anything, so bare `codeact`
prints them together and points at the command that changes each one.
"""

from __future__ import annotations

import argparse

from .. import commands, config, paths, proposals, registry, secrets_store


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "status", help="what exists, what's pending, what's enforced"
    )
    parser.set_defaults(handler=run)


def _corpus_size() -> int:
    try:
        with paths.corpus_path().open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _line(label: str, text: str) -> None:
    print(f"{label:<11}{text}")


def run(_args: argparse.Namespace) -> int:
    settings = config.load()
    reg = registry.Registry().load()
    entries = list(reg.entries.values())
    quarantined = [e for e in entries if e.quarantined]
    pending = proposals.listing(proposals.PENDING)

    print(f"library at {paths.root()}\n")

    detail = f"{len(entries)} helper(s)"
    if quarantined:
        detail += f", {len(quarantined)} quarantined ({', '.join(e.name for e in quarantined)})"
    if reg.errors:
        detail += f", {len(reg.errors)} failed to load"
    _line("helpers", detail + "   `codeact card --index`")

    if pending:
        escalating = sum(1 for p in pending if p.diff.get("escalates"))
        note = f", {escalating} increasing a helper's reach" if escalating else ""
        _line("proposals", f"{len(pending)} awaiting a decision{note}   `codeact review`")
    else:
        _line("proposals", "nothing pending")

    guard = str(settings.get("guard"))
    granted = ", ".join(settings.get("granted") or []) or "none"
    _line(
        "guard",
        f"{guard}"
        + (" — findings are recorded, nothing is blocked" if guard != "enforce" else "")
        + f"   granted: {granted}",
    )

    policy = commands.Policy.load(settings)
    governed = ", ".join(policy.rules) or "no rules"
    _line(
        "commands",
        f"{policy.mode} — {governed}"
        + ("" if policy.active else " (would be governed)")
        + "   `codeact policy`",
    )

    runner = settings.get("run_as")
    _line(
        "run_as",
        f"{runner} — kernel-enforced isolation   `codeact sandbox`"
        if runner
        else "not set; the interpreter runs as you, the same as Bash   `codeact sandbox`",
    )

    known = set(secrets_store.names())
    declared = {s for e in entries for s in e.card.meta.requires_secrets}
    missing = sorted(declared - known)
    _line(
        "secrets",
        f"{len(known)} set"
        + (f", {len(missing)} declared but unset: {', '.join(missing)}" if missing else "")
        + "   `codeact secret`",
    )

    size = _corpus_size()
    _line(
        "corpus",
        f"{size} block(s) recorded" + ("   `codeact mine`" if size else " — nothing to mine yet"),
    )
    return 0
