"""The MCP tool surface."""

from __future__ import annotations

import time

from . import corpus, guard, paths
from .interpreter import Session, Timeout
from .protocol import Server

VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0

_session: Session | None = None


def session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def _format(payload: dict, findings: list) -> str:
    """Compact, readable result. Empty sections are omitted entirely."""
    parts: list[str] = []

    if payload.get("stdout"):
        parts.append(payload["stdout"].rstrip())
    if payload.get("stderr"):
        parts.append("stderr:\n" + payload["stderr"].rstrip())
    if payload.get("error"):
        parts.append(payload["error"].rstrip())
    if payload.get("result") is not None:
        parts.append(f"→ {payload['result']}")

    delta = payload.get("delta") or []
    if delta:
        marks = {"new": "+", "changed": "~"}
        names = ", ".join(
            f"{marks[d['kind']]}{d['name']} ({d['type']}) {d['repr']}" for d in delta
        )
        parts.append("state: " + names)

    if payload.get("truncated"):
        parts.append("note: output truncated; the full text is in `_out`")

    note = guard.summarize(findings)
    if note:
        parts.append(note)

    return "\n\n".join(parts) if parts else "(no output)"


def build() -> Server:
    server = Server("codeact", VERSION)

    @server.tool(
        "run_python",
        (
            "Execute Python in a persistent session-scoped interpreter and return its "
            "output. State survives between calls: variables, imports and function "
            "definitions stay live, so load data once and keep working on it rather "
            "than re-reading it every call. Large intermediates should stay as "
            "variables — print a summary, not the whole object. Prefer this over "
            "shelling out to `python -c` for anything multi-step. The response lists "
            "names that were created or rebound, so you can track state without "
            "printing it."
        ),
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python source. The value of a trailing expression is reported, "
                        "REPL-style."
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "description": f"Seconds before interrupting. Default {DEFAULT_TIMEOUT:.0f}.",
                },
            },
            "required": ["code"],
        },
    )
    def run_python(code: str, timeout_s: float = DEFAULT_TIMEOUT) -> str:
        timeout = max(1.0, min(float(timeout_s), MAX_TIMEOUT))
        findings = guard.scan(code)
        started = time.monotonic()
        try:
            payload = session().execute(code, timeout)
        except Timeout as exc:
            corpus.record(
                code,
                outcome="timeout",
                duration_ms=int((time.monotonic() - started) * 1000),
                guard_findings=findings,
                error_type="Timeout",
            )
            return f"timeout: {exc}"

        elapsed = int((time.monotonic() - started) * 1000)
        error = payload.get("error")
        corpus.record(
            code,
            outcome="error" if error else "ok",
            duration_ms=elapsed,
            guard_findings=findings,
            error_type=error.strip().split(":")[0].splitlines()[-1] if error else None,
        )
        return _format(payload, findings)

    @server.tool(
        "session_state",
        (
            "List every name currently bound in the interpreter, with its type and a "
            "short repr. Use this to re-orient after a long stretch of work, or to "
            "check what survived an error, without printing the objects themselves."
        ),
        {"type": "object", "properties": {}},
    )
    def session_state() -> str:
        try:
            payload = session().state()
        except Timeout as exc:
            return f"timeout: {exc}"
        names = payload.get("names") or []
        if not names:
            return "The interpreter namespace is empty."
        lines = [f"{n['name']} ({n['type']}) {n['repr']}" for n in names]
        return f"{len(lines)} name(s) bound:\n" + "\n".join("  " + line for line in lines)

    @server.tool(
        "restart_session",
        (
            "Discard the interpreter and start a fresh one. Every variable, import and "
            "definition is lost, so use it only to recover from a wedged session or to "
            "deliberately clear state."
        ),
        {"type": "object", "properties": {}},
    )
    def restart_session() -> str:
        session().restart()
        return "Interpreter restarted. The namespace is empty."

    return server


def main() -> None:
    paths.ensure()
    try:
        build().run()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if _session is not None:
            _session.stop()
