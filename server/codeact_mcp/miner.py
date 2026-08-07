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

from . import linkage, paths

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


def call_sequence(tree: ast.AST, names: set) -> tuple[str, ...]:
    """The helper calls in a block, in execution order — the recurring *path*
    through the library, which is a different signal from any block's shape.

    Execution order, not source order: `trim(shout(x))` runs shout first, and
    it has to match the same path written as two statements or composition
    styles would split one habit into two clusters. A call is emitted after
    its arguments, which is when it actually happens. Immediate repeats are
    collapsed — a loop calling one helper five times is one step, not five.
    """
    ordered: list[str] = []

    class _Calls(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            self.visit(node.func)
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword)
            if isinstance(node.func, ast.Name) and node.func.id in names:
                ordered.append(node.func.id)

    _Calls().visit(tree)
    return tuple(n for i, n in enumerate(ordered) if i == 0 or n != ordered[i - 1])


def helper_sequences(reg) -> dict:
    """Each helper's own call path through the library, for recognising a
    block that re-walks a path some helper already packages."""
    out: dict = {}
    names = set(reg.entries)
    for entry in reg.entries.values():
        try:
            tree = ast.parse(entry.path.read_text())
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entry.name:
                seq = call_sequence(node, names - {entry.name})
                if len(seq) >= 2:
                    out[seq] = entry.name
    return out


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
    """The five review queues."""
    records = read_corpus()
    known, signatures = helper_fingerprints(reg)
    packaged = helper_sequences(reg)
    helper_names = set(reg.entries)

    clusters: dict[str, Cluster] = {}
    retrieval_failures: dict[str, Cluster] = {}
    seq_clusters: dict[tuple, Cluster] = {}

    for record in records:
        code = record.get("code") or ""
        if record.get("outcome") == "source_read":
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue

        # The second cluster type (§12): a repeated sequence of helper calls.
        # Every step is already a reviewed unit, which makes this the
        # strongest candidate signal available — the only open question about
        # such a cluster is whether the path deserves a name. Extracted before
        # the size cutoff below, because three calls of glue is exactly the
        # block the fingerprint is right to ignore and this queue exists for.
        seq = call_sequence(tree, helper_names)
        if len(seq) >= 2:
            cluster = seq_clusters.get(seq)
            if cluster is None:
                cluster = seq_clusters[seq] = Cluster(
                    digest="seq:" + "→".join(seq), code=code, size=len(seq)
                )
                # A path some helper already walks is a retrieval failure in
                # sequence form: the composition exists, discovery missed it.
                cluster.helper = packaged.get(seq, "")
            cluster.count += 1
            cluster.sessions.add(record.get("session"))
            cluster.projects.add(record.get("project") or "")
            if record.get("outcome") in ("error", "timeout"):
                cluster.failures += 1
            # And not also a shape candidate: both queues would be describing
            # the same block for the same decision, and the sequence — whose
            # every step is already a reviewed unit — is the sharper half.
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

    sequences = [
        c for seq, c in seq_clusters.items()
        if len(c.sessions) >= min_sessions and not c.helper
    ]
    sequences.sort(key=lambda c: -c.score())
    seq_failures = [
        c for c in seq_clusters.values()
        if len(c.sessions) >= min_sessions and c.helper
    ]

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

    # Helper-to-helper calls count as calls: a shared helper's sessions-level
    # usage may be zero precisely because everything reaches it through its
    # dependents, and retiring it out from under them is the one wrong answer.
    depended: set[str] = set()
    for e in reg.entries.values():
        depended.update(e.card.meta.uses)
        depended.update(linkage.helper_imports(e.path))

    removals = [
        e.name
        for e in reg.entries.values()
        if not e.builtin and e.name not in stats and e.name not in depended
    ]

    return {
        "candidates": [c.as_json() for c in candidates[:25]],
        "sequences": [
            {**c.as_json(), "chain": c.digest.removeprefix("seq:").split("→")}
            for c in sequences[:25]
        ],
        "retrieval_failures": [
            {**c.as_json(), "helper": c.helper}
            for c in sorted(
                [*retrieval_failures.values(), *seq_failures], key=lambda c: -c.count
            )[:25]
        ],
        "revisions": revisions[:25],
        "removals": sorted(removals)[:25],
        "corpus_size": len(records),
    }


# -- the budget, and the deferral rule ------------------------------------
#
# Open question 1, answered in two halves. The synthesis pass downstream of a
# mine costs human attention (and model calls) proportional to cluster count,
# so a run is capped. And a cluster a human has seen several times and not
# acted on has been answered, just not in writing — it is parked until its
# evidence grows beyond what it had when last shown, at which point the case
# for it is genuinely new and it earns a fresh hearing.


def _ledger_path():
    return paths.root() / "mining.json"


def read_ledger() -> dict:
    try:
        data = json.loads(_ledger_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_ledger(data: dict) -> None:
    try:
        paths.ensure()
        _ledger_path().write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def budgeted(
    q: dict, budget: int | None = None, defer_after: int | None = None, remember: bool = False
) -> dict:
    """Apply the per-run budget and the deferral rule to a queues() result.

    `remember=True` records what was actually shown, so only a real surfacing
    counts against a cluster — the review app browsing the same queues passes
    False and burns nothing. A budget of 0 means uncapped. Over-budget items
    are not parked, merely deferred to the next run: nobody has seen them, so
    nothing about them has been decided.
    """
    from . import config

    settings = config.get("mine") or {}
    if budget is None:
        budget = int(settings.get("budget") or 0)
    if defer_after is None:
        defer_after = int(settings.get("defer_after") or 3)

    ledger = read_ledger()
    current = {item["digest"] for key in ("candidates", "sequences") for item in q.get(key) or []}

    parked = 0
    for key in ("candidates", "sequences"):
        kept = []
        for item in q.get(key) or []:
            entry = ledger.get(item["digest"])
            grown = entry is None or (
                item["count"] > entry.get("count", 0)
                or item["sessions"] > entry.get("sessions", 0)
            )
            if entry is not None and entry.get("surfaced", 0) >= defer_after and not grown:
                parked += 1
                continue
            kept.append(item)
        q[key] = kept

    if budget > 0:
        pool = sorted(
            ((item["score"], key, item["digest"]) for key in ("candidates", "sequences") for item in q[key]),
            key=lambda t: -t[0],
        )
        keep = {(key, digest) for _, key, digest in pool[:budget]}
        for key in ("candidates", "sequences"):
            q[key] = [item for item in q[key] if (key, item["digest"]) in keep]

    if remember:
        ledger = {k: v for k, v in ledger.items() if k in current}
        for key in ("candidates", "sequences"):
            for item in q[key]:
                entry = ledger.setdefault(item["digest"], {"surfaced": 0})
                if item["count"] > entry.get("count", 0) or item["sessions"] > entry.get("sessions", 0):
                    entry["surfaced"] = 0  # new evidence restarts the clock
                entry["surfaced"] = entry.get("surfaced", 0) + 1
                entry["count"] = item["count"]
                entry["sessions"] = item["sessions"]
        write_ledger(ledger)

    q["parked"] = parked
    return q
