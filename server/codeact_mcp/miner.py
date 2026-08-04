"""Offline mining over the corpus of everything ever executed.

In-session proposal asks the agent to interrupt a task to do library
maintenance, which it mostly won't — and the strongest evidence for a helper is
repetition *across* sessions, which is invisible from inside any one of them. So
this is the systematic path, and it runs out of the hot path.

It emits four queues, not one. New candidates are the obvious output; the
valuable one is retrieval failures — inline code matching a helper that already
exists, which means the library was fine and discovery broke. That distinction
is otherwise invisible and the two problems need completely different fixes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import paths

# Below this, a "pattern" is a line of glue and not worth a function.
MIN_NODES = 12
MIN_SESSIONS = 2

_KEEP = frozenset(
    """
    abs all any ast base64 bool bytes collections csv datetime dict enumerate
    filter float functools getattr hashlib int io itertools json len list map
    math max min open os pathlib print random range re requests round set
    setattr sorted statistics str subprocess sum textwrap time tuple urllib zip
    """.split()
)


class _Canon(ast.NodeTransformer):
    """Rename locals positionally and reduce literals to their types.

    This is what makes the fingerprint catch same-shape-different-names, which
    is the common case and exactly what hashing the text misses. Module and
    builtin names survive, because `json.loads` and `socket.connect` are the
    signal rather than the noise.
    """

    def __init__(self) -> None:
        self.names: dict[str, int] = {}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in _KEEP:
            return node
        index = self.names.setdefault(node.id, len(self.names))
        return ast.copy_location(ast.Name(id=f"v{index}", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        index = self.names.setdefault(node.arg, len(self.names))
        node.arg = f"v{index}"
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)

    def visit_Return(self, node: ast.Return) -> ast.AST:
        # A helper body ends `return x`; the same logic written inline ends with
        # a bare `x`. Without collapsing the two, a helper could never match its
        # own inline re-implementation and the retrieval-failure queue would
        # never fire.
        self.generic_visit(node)
        if node.value is None:
            return ast.copy_location(ast.Pass(), node)
        return ast.copy_location(ast.Expr(value=node.value), node)


def fingerprint(code: str) -> tuple[str, int]:
    """A structural hash of a block, plus its size. ("" , 0) if unusable."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "", 0
    return fingerprint_tree(tree)


def fingerprint_tree(tree: ast.AST) -> tuple[str, int]:
    """Fingerprint an already-parsed tree.

    Helper bodies are hashed this way rather than round-tripped through
    unparse-then-parse, because a body containing `return` is not valid at
    module level and would silently fingerprint as nothing.
    """
    size = sum(1 for _ in ast.walk(tree))
    if size < MIN_NODES:
        return "", size
    canonical = _Canon().visit(tree)
    ast.fix_missing_locations(canonical)
    dumped = ast.dump(canonical, annotate_fields=False)
    return hashlib.sha256(dumped.encode()).hexdigest()[:16], size


def helper_fingerprints(reg) -> tuple[dict[str, str], dict[str, frozenset]]:
    """Structural hashes and call signatures for every helper body."""
    out: dict[str, str] = {}
    signatures: dict[str, frozenset] = {}
    for entry in reg.entries.values():
        try:
            source = entry.path.read_text()
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entry.name:
                statements = node.body
                # Drop the docstring. It is part of the body, and leaving it in
                # would mean no inline re-implementation could ever match — which
                # would silently disable the retrieval-failure queue entirely,
                # the one output that distinguishes "we need another helper"
                # from "the agent could not find the one we have".
                if (
                    statements
                    and isinstance(statements[0], ast.Expr)
                    and isinstance(statements[0].value, ast.Constant)
                    and isinstance(statements[0].value.value, str)
                ):
                    statements = statements[1:]
                if not statements:
                    continue
                body = ast.Module(body=statements, type_ignores=[])
                digest, _ = fingerprint_tree(body)
                if digest:
                    out[digest] = entry.name
                signature = call_signature(body)
                if len(signature) >= 2:
                    signatures[entry.name] = signature
    return out, signatures


def call_signature(tree: ast.AST) -> frozenset:
    """The set of things a block calls, qualified where possible.

    Exact structural fingerprints have high precision and limited recall: a
    helper parameterised on `sep` will not match an inline copy that hardcodes
    `"-"`, because a Name and a Constant are genuinely different shapes. This is
    the design's second pass — what a block *calls* survives that kind of
    variation, so it catches re-implementations the hash cannot.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            base = target.value
            prefix = base.id + "." if isinstance(base, ast.Name) and base.id in _KEEP else ""
            names.add(prefix + target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return frozenset(names)


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def read_corpus(limit: int = 50000) -> list[dict]:
    path = paths.corpus_path()
    if not path.exists():
        return []
    entries = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-limit:]


@dataclass
class Cluster:
    digest: str
    code: str
    count: int = 0
    sessions: set = field(default_factory=set)
    projects: set = field(default_factory=set)
    failures: int = 0
    size: int = 0
    helper: str = ""

    def score(self) -> float:
        """What actually predicts a good helper.

        Frequency alone is a bad signal: five uses in one session is one task.
        Spread across sessions is a habit, and spread across *projects* is
        unambiguously a library function. Code that had to be fixed before it
        worked is worth more, because the helper bakes in a correction that was
        expensive to find once.
        """
        return (
            2.5 * len(self.projects)
            + 1.5 * len(self.sessions)
            + 0.5 * self.count
            + 1.0 * min(self.failures, 3)
        )

    def as_json(self) -> dict:
        return {
            "digest": self.digest,
            "code": self.code,
            "count": self.count,
            "sessions": len(self.sessions),
            "projects": sorted(self.projects),
            "failures": self.failures,
            "score": round(self.score(), 2),
        }


_usage_cache: tuple = ()


def usage(reg, cached: bool = True) -> dict[str, dict]:
    """Per-helper call statistics, for the retrieval priors.

    Approximated by name occurrence in executed code — the interpreter does not
    instrument calls, and inferring from the corpus is cheap and good enough for
    a ranking signal.
    """
    global _usage_cache
    try:
        stamp = (paths.corpus_path().stat().st_size, len(reg.entries))
    except OSError:
        stamp = (0, len(reg.entries))
    if cached and _usage_cache and _usage_cache[0] == stamp:
        return _usage_cache[1]

    stats: dict[str, dict] = {}
    entries = read_corpus()
    names = list(reg.entries)
    patterns = {name: re.compile(rf"\b{re.escape(name)}\s*\(") for name in names}
    for record in entries:
        code = record.get("code") or ""
        for name in names:
            if not patterns[name].search(code):
                continue
            slot = stats.setdefault(name, {"calls": 0, "failures": 0, "projects": set()})
            slot["calls"] += 1
            slot["projects"].add(record.get("project") or "")
            if record.get("outcome") == "error":
                slot["failures"] += 1
    for slot in stats.values():
        slot["projects"] = sorted(slot["projects"])
    _usage_cache = (stamp, stats)
    return stats


def queues(reg, min_sessions: int = MIN_SESSIONS) -> dict[str, Any]:
    """The four review queues."""
    records = read_corpus()
    known, signatures = helper_fingerprints(reg)

    clusters: dict[str, Cluster] = {}
    retrieval_failures: dict[str, Cluster] = {}

    for record in records:
        code = record.get("code") or ""
        if record.get("outcome") == "source_read":
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        digest, size = fingerprint_tree(tree)
        if not digest:
            continue

        matched = known.get(digest, "")
        if not matched:
            # Second pass: a block that calls the same distinctive things a
            # helper calls, without calling the helper itself, is very likely a
            # re-implementation the exact hash could not see.
            calls = call_signature(tree)
            for name, signature in signatures.items():
                if name in calls:
                    break  # it used the helper — not a re-implementation
                if _overlap(calls, signature) >= 0.7:
                    matched = name
                    break

        bucket = retrieval_failures if matched else clusters
        cluster = bucket.get(digest)
        if cluster is None:
            cluster = bucket[digest] = Cluster(digest=digest, code=code, size=size)
            cluster.helper = matched
        cluster.count += 1
        cluster.sessions.add(record.get("session"))
        cluster.projects.add(record.get("project") or "")
        if record.get("outcome") in ("error", "timeout"):
            cluster.failures += 1

    candidates = [
        c for c in clusters.values() if len(c.sessions) >= min_sessions
    ]
    candidates.sort(key=lambda c: -c.score())

    stats = usage(reg)
    revisions = [
        {
            "name": name,
            "calls": slot["calls"],
            "failures": slot["failures"],
            "rate": round(slot["failures"] / slot["calls"], 2),
        }
        for name, slot in stats.items()
        if slot["calls"] >= 3 and slot["failures"] / slot["calls"] > 0.3
    ]
    revisions.sort(key=lambda r: -r["rate"])

    # Quarantined helpers are the other revision trigger, and the urgent one:
    # something is currently broken rather than merely unreliable.
    for entry in reg.entries.values():
        if entry.quarantined:
            revisions.insert(
                0, {"name": entry.name, "quarantined": entry.card.quarantine}
            )

    removals = [
        e.name
        for e in reg.entries.values()
        if not e.builtin and e.name not in stats
    ]

    return {
        "candidates": [c.as_json() for c in candidates[:25]],
        "retrieval_failures": [
            {**c.as_json(), "helper": c.helper}
            for c in sorted(retrieval_failures.values(), key=lambda c: -c.count)[:25]
        ],
        "revisions": revisions[:25],
        "removals": sorted(removals)[:25],
        "corpus_size": len(records),
    }
