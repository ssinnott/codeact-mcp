"""`codeact trace` — what an interpreter session actually did.

    codeact trace                  every session on this machine, newest first
    codeact trace last             the most recent one, as a transcript
    codeact trace a1b2c3 --code    just the Python, as a script you can re-run

The transcript is the whole exchange: code in, output back, state delta, guard
findings, restarts, grants. `--code` throws all of that away on purpose — for
reproducing a session rather than reading it, the answers are noise and the
questions are the artefact.
"""

from __future__ import annotations

import argparse
import time

from .. import paths, trace

# Enough to see the shape of an output without a 4000-character print flooding
# the terminal. --full is the escape hatch, and the file is always right there.
# Both bounds are needed: the repr of a 500-element list is one enormous line,
# so a line count alone would let exactly the worst case through untouched.
PREVIEW_LINES = 12
PREVIEW_WIDTH = 160


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "trace", help="what an interpreter session ran, after the fact"
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="session id, a unique prefix of one, or `last`. Omit to list them.",
    )
    parser.add_argument(
        "--code", action="store_true", help="print only the Python, as a script"
    )
    parser.add_argument(
        "--full", action="store_true", help="don't abbreviate long output"
    )
    parser.add_argument(
        "-n", type=int, default=0, metavar="N", help="only the last N events"
    )
    parser.add_argument(
        "--prune",
        type=int,
        metavar="DAYS",
        help="delete traces older than DAYS and stop",
    )
    parser.set_defaults(handler=run)


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _secs(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _indent(text: str, prefix: str, full: bool) -> list[str]:
    lines = text.rstrip().splitlines()
    if not full:
        if len(lines) > PREVIEW_LINES:
            dropped = len(lines) - PREVIEW_LINES
            lines = lines[:PREVIEW_LINES] + [f"… {dropped} more line(s) — --full to see them"]
        lines = [
            line
            if len(line) <= PREVIEW_WIDTH
            else f"{line[:PREVIEW_WIDTH]}… (+{len(line) - PREVIEW_WIDTH} chars)"
            for line in lines
        ]
    return [prefix + line for line in lines]


def _tally(s: dict) -> str:
    """`2 errors`, not `2 error` — this is read, not parsed."""
    parts = []
    for label, key in (("error", "errors"), ("blocked", "blocked"), ("timeout", "timeouts")):
        if s[key]:
            parts.append(f"{s[key]} {label}" + ("s" if s[key] != 1 and label != "blocked" else ""))
    return ", ".join(parts)


# ---------------------------------------------------------------------------


def _list() -> int:
    found = trace.files()
    if not found:
        print(f"no traces yet ({paths.traces_dir()})")
        print("\nOne is written per interpreter session. Turn it off with")
        print('`trace: false` in ~/.codeact/config.json.')
        return 0

    print(f"traces in {paths.traces_dir()}\n")
    for target in found:
        s = trace.summarize(trace.read(target))
        counts = f"{s['runs']} run(s)"
        if _tally(s):
            counts += ", " + _tally(s)
        if not s["complete"]:
            counts += ", ended abruptly"
        print(f"  {s['session']:<14}{_stamp(s['started']):<18}{counts}")
        print(f"  {'':<14}{s['project']}")
    print("\n`codeact trace <id>` for the transcript, `--code` for just the Python.")
    return 0


def _header(s: dict) -> None:
    print(f"session {s['session']}")
    print(f"  started   {_stamp(s['started'])}")
    print(f"  project   {s['project']}")
    rules = [f"guard {s['guard']}", f"commands {s['commands']}"]
    rules.append(f"run_as {s['run_as']}" if s["run_as"] else "run_as unset")
    if s["granted"]:
        rules.append("granted " + ", ".join(s["granted"]))
    print("  rules     " + " · ".join(rules))
    tally = f"{s['runs']} block(s)"
    if _tally(s):
        tally += ", " + _tally(s)
    if s["restarts"]:
        tally += f", {s['restarts']} restart(s)"
    print(f"  ran       {tally}, {_secs(s['duration_ms'])} inside the interpreter")
    if not s["complete"]:
        print("  note      the session did not close cleanly; this may be truncated")
    print()


def _event(e: dict, full: bool) -> None:
    kind = e.get("event")
    when = _clock(e.get("ts", 0))

    if kind == "run":
        outcome = e.get("outcome", "?")
        print(f"[{e.get('n')}] {when}  {outcome}  {_secs(int(e.get('duration_ms') or 0))}")
        for line in _indent(e.get("code") or "", "    ", full):
            print(line)
        if e.get("stdout"):
            for line in _indent(e["stdout"], "  | ", full):
                print(line)
        if e.get("stderr"):
            for line in _indent(e["stderr"], "  ! ", full):
                print(line)
        if e.get("error"):
            for line in _indent(e["error"], "  ! ", full):
                print(line)
        if e.get("result") is not None:
            for line in _indent(str(e["result"]), "  → ", full):
                print(line)
        if e.get("delta"):
            marks = {"new": "+", "changed": "~"}
            names = ", ".join(
                f"{marks.get(d.get('kind'), '?')}{d.get('name')} ({d.get('type')}) {d.get('repr')}"
                for d in e["delta"]
            )
            for line in _indent("state: " + names, "  ", full):
                print(line)
        if e.get("guard"):
            tiers = ", ".join(f"{g.get('name')} (tier {g.get('tier')})" for g in e["guard"])
            print(f"  guard: {tiers}")
        if e.get("note"):
            print(f"  note: {e['note']}")
        if e.get("truncated"):
            print("  note: output was truncated before the session saw it")
        print()
        return

    if kind == "state":
        print(f"[{e.get('n')}] {when}  inspected the namespace — {e.get('names')} name(s)\n")
    elif kind == "restart":
        print(f"[{e.get('n')}] {when}  RESTART ({e.get('reason')}) — the namespace was lost\n")
    elif kind == "grant":
        print(f"[{e.get('n')}] {when}  granted {e.get('capability')} — {e.get('reason')}\n")
    elif kind == "source":
        print(f"[{e.get('n')}] {when}  read the source of {e.get('helper')} — {e.get('reason')}\n")
    elif kind == "end":
        print(f"[{e.get('n')}] {when}  session ended\n")


def _script(events: list[dict], s: dict) -> None:
    """Only what ran, in order, annotated with how it went.

    Blocked and timed-out blocks stay in and stay commented out: leaving them
    out would produce a script that runs clean and misrepresents the session.
    """
    print(f"# session {s['session']} — {_stamp(s['started'])}")
    print(f"# {s['project']}")
    print(f"# {s['runs']} block(s); this is the code only, replayed in order.\n")
    for e in events:
        if e.get("event") == "restart":
            print(f"# --- interpreter restarted ({e.get('reason')}); state lost ---\n")
            continue
        if e.get("event") != "run":
            continue
        outcome = e.get("outcome")
        print(f"# [{e.get('n')}] {outcome}, {_secs(int(e.get('duration_ms') or 0))}")
        code = (e.get("code") or "").rstrip()
        if outcome in ("blocked", "timeout"):
            print(f"# this block did not complete ({outcome}):")
            for line in code.splitlines():
                print("# " + line)
        else:
            print(code)
        print()


def _prune(days: int) -> int:
    cutoff = time.time() - days * 86400
    removed = 0
    for target in trace.files():
        try:
            if target.stat().st_mtime < cutoff:
                target.unlink()
                removed += 1
        except OSError:
            continue
    print(f"removed {removed} trace(s) older than {days} day(s)")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.prune is not None:
        return _prune(args.prune)
    if not args.session:
        return _list()

    target = trace.find(args.session)
    if target is None:
        available = [p.stem for p in trace.files()]
        hint = f" Known: {', '.join(available[:8])}" if available else ""
        print(f"no trace matching {args.session!r}.{hint}")
        return 1

    events = trace.read(target)
    if not events:
        print(f"{target} is empty")
        return 1

    s = trace.summarize(events)
    body = [e for e in events if e.get("event") != "session"]
    if args.n:
        body = body[-args.n :]

    if args.code:
        _script(body, s)
        return 0

    _header(s)
    for e in body:
        _event(e, args.full)
    print(f"({target})")
    return 0
