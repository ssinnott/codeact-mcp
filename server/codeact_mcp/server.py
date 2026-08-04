"""The MCP tool surface."""

from __future__ import annotations

import os
import time

from . import (
    config,
    corpus,
    guard,
    miner,
    paths,
    proposals,
    search as search_mod,
    secrets_store,
    taxonomy,
)
from .interpreter import Session, Timeout
from .protocol import Server
from .registry import registry

VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0

_session: Session | None = None
# Capabilities the human granted for this session only. Never persisted — the
# point of a session grant is that it expires.
_session_grants: set[str] = set()


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

    text = "\n\n".join(parts) if parts else "(no output)"
    # Egress redaction. Tracebacks are the case that matters: an exception
    # raised inside an HTTP library will print the auth header it was called
    # with, and that path bypasses the Secret wrapper entirely.
    return secrets_store.redact(text)


def _error_type(error: str | None) -> str | None:
    """The exception class from a formatted traceback.

    It is on the LAST line, not the first — splitting the whole traceback on its
    first colon yields "Traceback (most recent call last)", which would make the
    corpus's error_type field useless for exactly the failure clustering the
    miner will want it for.
    """
    if not error:
        return None
    last = error.strip().splitlines()[-1]
    return last.split(":", 1)[0].strip() or None


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

        if config.enforcing():
            granted = set(config.get("granted") or ()) | _session_grants
            blocked = guard.blocking(findings, granted)
            if blocked:
                corpus.record(
                    code,
                    outcome="blocked",
                    duration_ms=0,
                    guard_findings=findings,
                    error_type="GuardBlocked",
                )
                return guard.refusal(blocked)

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
            error_type=_error_type(error),
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
        "search_helpers",
        (
            "Find approved helper functions relevant to a task. Check this before "
            "writing non-trivial code — the library is where solved problems live, and "
            "rewriting one of them by hand is wasted work. Returns one line per helper: "
            "signature, summary, and its job/domain tags. Call describe_helper for the "
            "full contract of anything that looks right.\n\n"
            "Classify the task yourself and pass the categories — jobs are: "
            + ", ".join(taxonomy.JOBS)
            + "."
        ),
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What you are trying to do, in your own words.",
                },
                "jobs": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(taxonomy.JOBS)},
                    "description": "Restrict to these job categories.",
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(taxonomy.DOMAINS)},
                    "description": "Restrict to these subject domains.",
                },
            },
        },
    )
    def search_helpers(
        task: str = "", jobs: list | None = None, domains: list | None = None
    ) -> str:
        reg = registry()
        found = search_mod.search(
            reg.available(),
            task=task,
            jobs=jobs or (),
            domains=domains or (),
            project=os.getcwd(),
            # Mined priors: how often each helper has actually been used, how
            # often it failed, and in how many projects.
            usage=miner.usage(reg),
        )
        if not found:
            counts = reg.counts_by_job()
            if not sum(counts.values()):
                return (
                    "The helper library is empty. Write plain Python for now.\n\n"
                    "Jobs available for future helpers:\n" + taxonomy.job_index()
                )
            shape = " · ".join(f"{j} ({n})" for j, n in sorted(counts.items()))
            return f"Nothing matched. The library holds: {shape}"
        lines = [e.card.index_line() for e in found]
        return (
            f"{len(lines)} helper(s), most relevant first:\n"
            + "\n".join("  " + line for line in lines)
            + "\n\nCall describe_helper(name) for the full contract before using one."
        )

    @server.tool(
        "describe_helper",
        (
            "Get the full usage contract for one helper: what it is for, when not to "
            "use it, every parameter, the shape of what it returns, how it fails, and "
            "worked examples whose output was captured from real runs. This contract is "
            "the complete interface — read it rather than guessing from the signature."
        ),
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The helper's name."}},
            "required": ["name"],
        },
    )
    def describe_helper(name: str) -> str:
        reg = registry()
        entry = reg.get(name)
        if entry is None:
            close = [n for n in reg.namespace() if name.lower() in n.lower()]
            hint = f" Did you mean: {', '.join(sorted(close))}?" if close else ""
            return f"No helper named {name!r}.{hint} Use search_helpers to look."
        return entry.card.render()

    @server.tool(
        "propose_helper",
        (
            "Propose a new helper for the library, or a revision to an existing one. "
            "This installs NOTHING — it records a pending proposal and runs the full "
            "validation gate, and a human decides.\n\n"
            "Propose when you have written the same non-trivial code more than once, "
            "or when the guard blocked a capability and packaging it as a reviewed "
            "helper is the right way to get it. The test: would a future session, with "
            "no memory of this task, be better off?\n\n"
            "`source` must be a complete module defining exactly one function decorated "
            "with @helper. The agent that later uses it will NEVER see this source — "
            "only the docstring — so the contract must be complete: a summary, "
            "'Use when:' and \"Don't use when:\" lines, every parameter under Args:, the "
            "SHAPE of the return under Returns:, failure modes under Raises:, and at "
            "least one runnable example whose real output is captured on approval.\n\n"
            + proposals.vocabulary_hint()
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The helper's function name."},
                "source": {
                    "type": "string",
                    "description": (
                        "Complete module source, starting with `from codeact import "
                        "helper`, defining one decorated function."
                    ),
                },
                "revises": {
                    "type": "string",
                    "description": (
                        "Name of an existing helper this replaces. Prefer adding an "
                        "optional argument over a breaking change — a breaking change "
                        "is a new helper with a new name, not a revision."
                    ),
                },
            },
            "required": ["name", "source"],
        },
    )
    def propose_helper(name: str, source: str, revises: str = "") -> str:
        proposal = proposals.propose(name, source, revises)
        if proposal.problems:
            return (
                f"Proposal {proposal.id} for {name!r} did NOT pass the gate. "
                f"Fix all of these and propose again:\n  "
                + "\n  ".join(proposal.problems)
            )
        note = ""
        if proposal.diff.get("escalates"):
            note = (
                "\n\nNote: this revision increases what the helper can reach, so it "
                "will need the same scrutiny as a brand-new privileged helper."
            )
        return (
            f"Proposal {proposal.id} for {name!r} passed the gate and is pending review.\n"
            f"A human approves it with `codeact review` or `python3 tools/approve.py "
            f"{proposal.id}`. It is not callable until then.{note}\n\n"
            f"Card as it will be seen:\n{proposal.card}"
        )

    @server.tool(
        "helper_source",
        (
            "Read a helper's implementation. This exists ONLY for revising an existing "
            "helper — read it, then call propose_helper(..., revises=<name>).\n\n"
            "Do not use it to work around a helper that misbehaves. If behaviour "
            "contradicts the card, that is a defect worth reporting, and quietly "
            "writing your own corrected copy hides it from everyone else."
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Why you need the source. Recorded with the request.",
                },
            },
            "required": ["name", "reason"],
        },
    )
    def helper_source(name: str, reason: str) -> str:
        entry = registry().get(name)
        if entry is None:
            return f"No helper named {name!r}."
        corpus.record(
            f"# helper_source({name!r})\n# reason: {reason}",
            outcome="source_read",
            duration_ms=0,
        )
        return f"# {entry.path}\n\n{entry.path.read_text()}"

    @server.tool(
        "request_capability",
        (
            "Ask the human to grant a privileged capability for this session only. "
            "Prefer propose_helper instead where the work is reusable: an approved "
            "helper carries its capability permanently and every later session "
            "benefits, while a grant expires when the session ends."
        ),
        {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "enum": ["network", "process", "filesystem", "deserialize", "dynamic"],
                },
                "reason": {
                    "type": "string",
                    "description": "What you need it for, specifically.",
                },
            },
            "required": ["capability", "reason"],
        },
        # Always prompts, and can never be pre-allowlisted away.
        **{"anthropic/requiresUserInteraction": True},
    )
    def request_capability(capability: str, reason: str) -> str:
        _session_grants.add(capability)
        return (
            f"Granted `{capability}` for this session only. It disappears when the "
            "session ends; propose_helper is how it becomes permanent."
        )

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
