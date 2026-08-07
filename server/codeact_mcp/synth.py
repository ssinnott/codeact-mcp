"""Synthesis: turning mined clusters into pending proposals, with a model.

The miner surfaces evidence; turning a cluster into a helper is a *writing*
task — a card, a contract, a general interface extracted from specific code —
and the model is the writer. This wraps the Claude Code CLI rather than an API
client, deliberately: the CLI is already present and authenticated wherever
this plugin runs, and the server stays pure standard library with no key to
manage.

Synthesis writes **proposals, never helpers**. Every draft goes through the
same gate `propose_helper` uses — validation, example execution, the effect
closure — and lands in the same review queue, so the human surface is
unchanged and nothing becomes callable without approval. The model drafting a
helper is no more trusted than the agent proposing one, which is to say: not
at all, until a human reads it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from . import proposals, taxonomy

DEFAULT_TIMEOUT = 300.0

# Markers rather than JSON: module source full of quotes and newlines survives
# a text envelope far better than a string literal, and a model that adds
# prose around its answer costs nothing when only the envelope is parsed.
_DRAFT = re.compile(
    r"=== HELPER ([A-Za-z_]\w*) ===\n(.*?)\n=== END HELPER ===", re.S
)
_FENCE = re.compile(r"^```(?:python)?\n(.*)\n```$", re.S)


def command() -> list[str]:
    """How to invoke the model, from config. The default is the claude CLI in
    headless mode, reading its prompt from stdin; overriding it is how a test
    substitutes a stub, and how a user picks a model or flags."""
    from . import config

    settings = config.get("mine") or {}
    return list(settings.get("synth_command") or ["claude", "-p"])


def available() -> str:
    """Empty if synthesis can run, otherwise the reason it cannot."""
    cmd = command()
    if not cmd:
        return "mine.synth_command is empty"
    if shutil.which(cmd[0]) is None:
        return (
            f"{cmd[0]!r} is not on PATH — synthesis wraps the Claude Code CLI, "
            "so install it or point mine.synth_command at something else"
        )
    return ""


def prompt_for(q: dict, index_lines: list[str]) -> str:
    """Everything the writer needs: the evidence, the contract, the vocabulary.

    The library index is included so the model can decline to duplicate what
    exists — the near-duplicate check in the gate would catch it anyway, but a
    draft that was never written costs less than one that was rejected.
    """
    lines = [
        "You maintain a curated library of Python helper functions for the",
        "codeact plugin. Below are code patterns that recurred across real",
        "sessions. For each cluster that deserves to become a reusable helper,",
        "write a complete module. Skip clusters that are too task-specific, too",
        "trivial, or already covered by an existing helper.",
        "",
        "Rules for every module:",
        "- Start with: from codeact import helper",
        "- Define exactly one function, decorated with",
        "  @helper(job=..., domains=[...], ...) and fully type-annotated.",
        f"- job is exactly one of: {', '.join(taxonomy.JOBS)}",
        f"- domains are one to three of: {', '.join(taxonomy.DOMAINS)}",
        f"- side_effects is one of: {', '.join(taxonomy.SIDE_EFFECTS)}",
        "- The docstring is the caller's ONLY interface — they never see the",
        "  source. It needs: a summary saying what the helper is for, a",
        "  'Use when:' line, a \"Don't use when:\" line, every parameter under",
        "  Args:, the SHAPE of the result under Returns: (say what is in it,",
        "  not just the type), and failure modes under Raises:.",
        "- At least one runnable example in examples=[{\"code\": \"...\"}] —",
        "  self-contained, using a setup= key if it needs fixtures. Examples",
        "  are executed and their real output is captured, so they must run.",
        "- Generalize what the evidence hardcodes (a separator, a path, a",
        "  field name) into parameters with sensible defaults.",
        "",
        "For a RECURRING SEQUENCE, write an orchestrate helper whose body is",
        "those calls: import each step with `from codeact.helpers import <name>`",
        "and declare every one in uses=[...] — an undeclared import is rejected.",
        "",
        "Output format — nothing else, no commentary outside the markers:",
        "=== HELPER <function_name> ===",
        "<the complete module source>",
        "=== END HELPER ===",
        "",
        "The library already holds (do not duplicate):",
    ]
    lines += [f"  {line}" for line in index_lines] or ["  (nothing yet)"]

    lines += ["", "## Candidate clusters"]
    if not q.get("candidates"):
        lines += ["(none)"]
    for i, c in enumerate(q.get("candidates") or [], 1):
        lines += [
            "",
            f"### Cluster {i}: seen {c['count']} times in {c['sessions']} "
            f"session(s), {len(c['projects'])} project(s), "
            f"{c['failures']} run(s) needed fixing first",
            "```python",
            c["code"],
            "```",
        ]

    lines += ["", "## Recurring sequences of existing helpers"]
    if not q.get("sequences"):
        lines += ["(none)"]
    for i, c in enumerate(q.get("sequences") or [], 1):
        lines += [
            "",
            f"### Sequence {i}: {' -> '.join(c['chain'])} — seen {c['count']} "
            f"times in {c['sessions']} session(s)",
            "```python",
            c["code"],
            "```",
        ]
    return "\n".join(lines)


def parse(text: str) -> list[tuple[str, str]]:
    """Every draft in the reply, tolerant of prose around the markers and of a
    model that fenced the source inside them."""
    drafts = []
    for match in _DRAFT.finditer(text or ""):
        source = match.group(2).strip()
        fenced = _FENCE.match(source)
        if fenced:
            source = fenced.group(1).strip()
        drafts.append((match.group(1), source + "\n"))
    return drafts


def run(q: dict, index_lines: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Ask the model to draft helpers for the queues, and gate every draft.

    Returns {"error": ...} when nothing could run, otherwise {"filed": [...]}
    with one gated Proposal per draft — pending if it passed, rejected with
    its problems if it did not. Either way the result is reviewable, which is
    the property that matters: synthesis must never have an outcome a human
    cannot see.
    """
    reason = available()
    if reason:
        return {"error": reason}
    if not (q.get("candidates") or q.get("sequences")):
        return {"error": "nothing to synthesize — the queues are empty"}

    prompt = prompt_for(q, index_lines)
    try:
        proc = subprocess.run(
            command(),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"the model did not answer within {timeout:.0f}s"}
    except OSError as exc:
        return {"error": f"could not run {command()[0]!r}: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-400:]
        return {"error": f"{command()[0]} exited {proc.returncode}: {detail}"}

    drafts = parse(proc.stdout)
    if not drafts:
        return {"error": "the reply contained no === HELPER === blocks"}

    return {"filed": [proposals.propose(name, source) for name, source in drafts]}
