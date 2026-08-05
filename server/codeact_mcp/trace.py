"""The transcript of one interpreter session.

The corpus records executed code for the miner: structure repeated across
sessions, deliberately without output, because fingerprints don't need it. That
answers "what patterns recur" — not "what did this session actually do", which
is the question being asked the moment a run goes wrong.

So: one file per session, in order, with what came back. Every exchange with the
interpreter lands here — the code, its stdout, its traceback, the namespace
delta, the guard's findings, the restarts, and the capabilities granted along
the way — under a header recording which rules were in force at the time. `what
was run` without `what was allowed` can't distinguish a block that was permitted
from one that would be refused today, so the rules are part of the record.

`codeact trace` reads it back.

Best effort throughout, like every other log here: a transcript that takes a
working session down is worse than no transcript at all.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths

# The trace and the corpus are two views of the same session, so they share an
# id — a candidate the miner surfaces can be traced back to the run it came from.
from .corpus import SESSION_ID, redact

# Backstop only. The worker already truncates what it hands back, so this bites
# on pathological code rather than on ordinary output.
MAX_FIELD = 8000

_context: dict = {}
_seq = 0
_enabled: bool | None = None


def path() -> Path:
    return paths.traces_dir() / f"{SESSION_ID}.jsonl"


def enabled() -> bool:
    """Read the setting once. The header records which mode was in force, so a
    mid-session config edit changing the answer would only make the file lie."""
    global _enabled
    if _enabled is None:
        try:
            from . import config

            _enabled = bool(config.get("trace"))
        except Exception:
            _enabled = True
    return _enabled


def begin(version: str) -> None:
    """Capture the session's context without writing anything yet.

    A server that starts and is never used should leave no file behind, so the
    header is emitted lazily, when the session first does something.
    """
    try:
        from . import config

        settings = config.load()
        _context.update(
            {
                "session": SESSION_ID,
                "version": version,
                "project": os.getcwd(),
                "pid": os.getpid(),
                "guard": settings.get("guard"),
                "granted": list(settings.get("granted") or ()),
                "run_as": settings.get("run_as"),
                "commands": (settings.get("commands") or {}).get("mode"),
            }
        )
    except Exception:
        pass


def _clip(text: str | None) -> str | None:
    """Redact, then bound. Output matters more here than in the corpus: a
    traceback out of an HTTP library prints the header it was called with."""
    if not text:
        return None
    text = redact(str(text))
    if len(text) <= MAX_FIELD:
        return text
    return text[:MAX_FIELD] + f"\n…[{len(text) - MAX_FIELD} more characters]"


def _write(event: str, fields: dict) -> None:
    global _seq
    if not enabled():
        return
    try:
        paths.ensure()
        target = path()
        # Reopened per event rather than held: a session that is killed — which
        # is the session most worth reading afterwards — must not lose its tail
        # to a buffer nobody flushed.
        with target.open("a", encoding="utf-8") as fh:
            if _seq == 0:
                _seq += 1
                header = {"n": 1, "ts": time.time(), "event": "session", **_context}
                fh.write(json.dumps(header) + "\n")
            _seq += 1
            fh.write(json.dumps({"n": _seq, "ts": time.time(), "event": event, **fields}) + "\n")
    except Exception:
        pass


def executed(
    code: str,
    *,
    outcome: str,
    duration_ms: int,
    timeout_s: float,
    findings: list | None = None,
    payload: dict | None = None,
    note: str | None = None,
) -> None:
    """One run_python call and everything the session saw come back from it."""
    payload = payload or {}
    fields: dict = {
        "code": _clip(code),
        "outcome": outcome,
        "duration_ms": duration_ms,
        "timeout_s": timeout_s,
    }
    if findings:
        fields["guard"] = [
            {"tier": f.tier, "kind": f.kind, "name": f.name} for f in findings
        ]
    for key in ("stdout", "stderr", "error", "result"):
        value = _clip(payload.get(key))
        if value:
            fields[key] = value
    if payload.get("delta"):
        fields["delta"] = [
            {**d, "repr": redact(str(d.get("repr", "")))} for d in payload["delta"]
        ]
    if payload.get("truncated"):
        fields["truncated"] = True
    if note:
        fields["note"] = _clip(note)
    _write("run", fields)


def inspected(count: int) -> None:
    _write("state", {"names": count})


def restarted(reason: str) -> None:
    _write("restart", {"reason": reason})


def granted(capability: str, reason: str) -> None:
    _write("grant", {"capability": capability, "reason": _clip(reason)})


def source_read(name: str, reason: str) -> None:
    _write("source", {"helper": name, "reason": _clip(reason)})


def end() -> None:
    if _seq:
        _write("end", {})


# ---------------------------------------------------------------------------
# Reading back


def files() -> list[Path]:
    """Every trace on the machine, newest first."""
    try:
        found = list(paths.traces_dir().glob("*.jsonl"))
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def read(target: Path) -> list[dict]:
    events = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a session killed mid-write leaves a partial last line
    return events


def find(name: str) -> Path | None:
    """Resolve `last`, a full session id, or an unambiguous prefix of one."""
    available = files()
    if not available:
        return None
    if name in ("last", "latest"):
        return available[0]
    exact = [p for p in available if p.stem == name]
    if exact:
        return exact[0]
    partial = [p for p in available if p.stem.startswith(name)]
    return partial[0] if len(partial) == 1 else None


def summarize(events: list[dict]) -> dict:
    """The shape of a session: how much ran, how much of it failed."""
    header = events[0] if events and events[0].get("event") == "session" else {}
    runs = [e for e in events if e.get("event") == "run"]
    return {
        "session": header.get("session", "?"),
        "started": header.get("ts") or (events[0].get("ts") if events else 0),
        "project": header.get("project", "?"),
        "guard": header.get("guard"),
        "granted": header.get("granted") or [],
        "run_as": header.get("run_as"),
        "commands": header.get("commands"),
        "version": header.get("version"),
        "runs": len(runs),
        "errors": sum(1 for e in runs if e.get("outcome") == "error"),
        "blocked": sum(1 for e in runs if e.get("outcome") == "blocked"),
        "timeouts": sum(1 for e in runs if e.get("outcome") == "timeout"),
        "restarts": sum(1 for e in events if e.get("event") == "restart"),
        "grants": [e.get("capability") for e in events if e.get("event") == "grant"],
        "duration_ms": sum(int(e.get("duration_ms") or 0) for e in runs),
        "complete": bool(events) and events[-1].get("event") == "end",
    }
