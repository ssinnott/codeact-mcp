"""The dependency edge: what a helper may import, and how that stays honest.

Until this module existed a helper file was an island, and not by design — it
was enforced by accident. Files load by path under a generated module name with
neither library directory importable, so a sibling import raised
`ModuleNotFoundError`, the registry filed the file under `errors`, and the
helper was simply absent from search. Two things follow from letting that edge
exist (§12), and both live here.

**Resolution.** An edge resolves through one name — `from codeact.helpers import
group_by` — so it is greppable and unambiguous, and the name resolves against
the user's library first and the shipped seeds second, the same shadowing rule
the registry already uses.

**The closure hash.** Quarantine fires when a helper's source stops matching the
source its examples were captured from. With an edge, editing a dependency
changes no helper file at all, so every dependent's card would silently become
unverified while still claiming it was checked — the exact failure quarantine
exists to prevent, arriving through the back door. Hashing the transitive
closure instead is what closes it.

Both readings come from the AST, never from importing the file: the file may be
the broken one, and the whole point is to be able to say so by name.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import paths

# A dependency that no longer resolves. Recorded rather than skipped so the
# closure hash still changes when it appears or disappears.
MISSING = "\x00missing\x00"

# The two layers an edge may point at (§12). The resolution and hashing below
# are written in terms of the layer, so both go through one mechanism.
LAYER_MODULES = {"codeact.helpers": "helpers", "codeact.core": "core"}

# What a loaded file is allowed to be named. The prefixes are load-bearing
# beyond tidiness: the secrets store identifies its caller by walking out to
# the first frame whose module carries the helper prefix, so a core module —
# named differently — cannot read a secret. That is §12's "core has no
# secrets" rule enforcing itself for free, intended rather than accidental.
LAYER_PREFIX = {"helpers": "codeact_helper_", "core": "codeact_core_"}


def layer_dirs(layer: str) -> tuple[Path, ...]:
    """Where a layer's files live, most specific first.

    User library before shipped seeds, so resolving an edge picks the same file
    the registry would have registered under that name.
    """
    if layer == "helpers":
        return (paths.helpers_dir(), paths.seeds_dir())
    if layer == "core":
        return (paths.core_dir(), paths.seeds_core_dir())
    return ()


# -- reading a file without running it -----------------------------------


@dataclass(frozen=True)
class Scan:
    """What a file declares and what it reaches for."""

    helpers: tuple[str, ...] = ()
    edges: tuple[tuple[str, str], ...] = ()


_EMPTY = Scan()
_scans: dict[tuple, Scan] = {}


def scan(path: Path) -> Scan:
    """Parse a file's declarations and edges, cached on its mtime and size."""
    try:
        stat = path.stat()
    except OSError:
        return _EMPTY
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _scans.get(key)
    if hit is None:
        hit = _scans[key] = _parse(path)
    return hit


def _parse(path: Path) -> Scan:
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, ValueError):
        # A file that does not parse declares nothing we can name. The registry
        # still reports the failure; there is just no helper to attach it to.
        return _EMPTY

    edges: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        layer = LAYER_MODULES.get(node.module)
        if layer:
            edges += [(layer, alias.name) for alias in node.names if alias.name != "*"]

    # Only module-level definitions: a decorated closure is not a library
    # helper, and the registry would not have registered it either.
    helpers = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_helper_decorator(d) for d in node.decorator_list)
    ]
    return Scan(tuple(helpers), tuple(dict.fromkeys(edges)))


def _is_helper_decorator(node: ast.expr) -> bool:
    """`@helper(...)` or `@codeact.helper(...)`, the two spellings in use."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr == "helper"
    return isinstance(target, ast.Name) and target.id == "helper"


def declared_helpers(path: Path) -> tuple[str, ...]:
    return scan(path).helpers


def edges(path: Path) -> tuple[tuple[str, str], ...]:
    return scan(path).edges


# -- resolution ----------------------------------------------------------


def resolve(layer: str, name: str) -> Path | None:
    """The file an edge points at, or None if nothing declares that name."""
    if not name.isidentifier():
        return None
    for directory in layer_dirs(layer):
        direct = directory / f"{name}.py"
        # A core name IS its file: there is no decorator to declare anything
        # else, so the filename is the whole convention.
        if layer == "core":
            if direct.is_file():
                return direct
            continue
        if direct.is_file() and name in scan(direct).helpers:
            return direct
        # A file may define more than one helper, so the convention that a
        # helper lives in `<name>.py` is a fast path, not the rule.
        for path in sorted(directory.glob("*.py")):
            if not path.name.startswith("_") and name in scan(path).helpers:
                return path
    return None


def helper_names() -> list[str]:
    """Every helper name reachable by an edge. Shadowed names appear once."""
    found: set[str] = set()
    for directory in layer_dirs("helpers"):
        for path in sorted(directory.glob("*.py")):
            if not path.name.startswith("_"):
                found.update(scan(path).helpers)
    return sorted(found)


def core_names() -> list[str]:
    """Every core module reachable by an edge. Shadowed names appear once."""
    found: set[str] = set()
    for directory in layer_dirs("core"):
        for path in directory.glob("*.py"):
            if not path.name.startswith("_"):
                found.add(path.stem)
    return sorted(found)


# -- loading -------------------------------------------------------------

# Names currently being executed, so a cycle is refused with the path that
# caused it instead of exhausting the stack. Cycles are also refused at the
# gate (§12), but the loader is what a hand-edited library hits first.
_loading: list[str] = []


def _plain(module_name: str) -> str:
    for prefix in LAYER_PREFIX.values():
        module_name = module_name.removeprefix(prefix)
    return module_name


def load_module(path: Path, layer: str = "helpers"):
    """Execute a library file under its layer's canonical module name.

    The prefix is load-bearing — see LAYER_PREFIX. Cycle detection spans both
    layers, since a helper importing a core module that imports the helper is a
    cycle no single layer's bookkeeping would see.
    """
    name = f"{LAYER_PREFIX[layer]}{path.stem}"
    if name in _loading:
        cycle = " -> ".join(_plain(m) for m in [*_loading, name])
        raise ImportError(f"import cycle in the library: {cycle}")

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    _loading.append(name)
    try:
        spec.loader.exec_module(module)
    finally:
        _loading.pop()
    return module


def load_helper(name: str) -> Callable[..., Any]:
    """Resolve `from codeact.helpers import <name>` to the function itself.

    A quarantined helper still resolves, deliberately. Quarantine means *this
    card's documented behaviour may be false*, and a caller does not read the
    card — it calls the code, and its own captured examples are what verify it.
    """
    path = resolve("helpers", name)
    if path is None:
        raise ImportError(
            f"no helper named {name!r} in the library — "
            "search_helpers() lists what exists"
        )
    fn = getattr(load_module(path), name, None)
    if fn is None:
        raise ImportError(f"{path.name} declares {name!r} but does not define it")
    return fn


def load_core(name: str):
    """Resolve `from codeact.core import <name>` to the module itself.

    A core name is a module, not a function: its audience is the author of a
    helper, who reads the source, so there is no card and nothing to search —
    which is exactly what distinguishes core from a helper (§12).
    """
    path = resolve("core", name)
    if path is None:
        raise ImportError(
            f"no core module named {name!r} — core modules live in "
            f"{paths.core_dir()} (or the plugin's seeds/core)"
        )
    return load_module(path, layer="core")


# -- the closure ---------------------------------------------------------


def closure(path: Path) -> list[tuple[str, str]]:
    """Every source this file's behaviour depends on, as sorted (name, source).

    Sorted by name rather than by traversal order, so the hash is stable across
    load order and across a dependency reached by two paths.
    """
    seen: dict[str, str] = {}

    def visit(target: Path, name: str) -> None:
        if name in seen:
            return
        try:
            seen[name] = target.read_text()
        except OSError:
            seen[name] = MISSING
            return
        for layer, dep in scan(target).edges:
            found = resolve(layer, dep)
            if found is None:
                seen.setdefault(f"{layer}/{dep}", MISSING)
            else:
                visit(found, f"{layer}/{found.stem}")

    visit(path, f"helpers/{path.stem}")
    return sorted(seen.items())


# -- effective reach -----------------------------------------------------


@dataclass(frozen=True)
class Reach:
    """What a helper can actually do: its own declarations plus everything it
    uses, transitively.

    Without this, composition is a laundering path (§12): wrap `fetch_url` in a
    function declaring `side_effects="none"` and the pure/world-touching
    boundary is gone — quietly, because the wrapper's own body contains nothing
    suspicious. The gate checks the job against these *effective* values, and
    the card shows where each one came from.
    """

    effects: tuple[tuple[str, str], ...] = ()  # (effect, via); via "" = declared here
    secrets: tuple[tuple[str, str], ...] = ()  # (secret, via); via "" = declared here
    uses: tuple[str, ...] = ()                 # the transitive helper closure
    missing: tuple[str, ...] = ()              # dependencies nothing declares
    cycle: tuple[str, ...] = ()                # a path back to the root, if one exists


def helper_imports(path: Path) -> tuple[str, ...]:
    """The helper names a file imports — the edges as the AST sees them."""
    return tuple(n for layer, n in scan(path).edges if layer == "helpers")


def reach(name: str, meta: Any, path: Path, entries: dict) -> Reach:
    """Compute a helper's effective reach against a loaded library.

    Dependencies are the union of what the card declares (`uses=`) and what the
    file imports: the gate reports a mismatch between the two separately, but
    reach must count both, because hidden reach that stopped counting would be
    the exact laundering this exists to prevent.
    """
    effects: dict[str, str] = {}
    secrets: dict[str, str] = {}
    if meta.side_effects != "none":
        effects[meta.side_effects] = ""
    for secret in meta.requires_secrets:
        secrets[secret] = ""

    parent_of: dict[str, str] = {}
    missing: list[str] = []
    cycle: tuple[str, ...] = ()
    queue = [(dep, name) for dep in dict.fromkeys([*meta.uses, *helper_imports(path)])]
    while queue:
        dep, parent = queue.pop(0)
        if dep == name:
            if not cycle:
                chain = []
                at = parent
                while at != name:
                    chain.append(at)
                    at = parent_of.get(at, name)
                cycle = (name, *reversed(chain), name)
            continue
        if dep in parent_of:
            continue
        parent_of[dep] = parent
        entry = entries.get(dep)
        if entry is None:
            missing.append(dep)
            continue
        dep_meta = entry.card.meta
        if dep_meta.side_effects != "none":
            effects.setdefault(dep_meta.side_effects, dep)
        for secret in dep_meta.requires_secrets:
            secrets.setdefault(secret, dep)
        for sub in dict.fromkeys([*dep_meta.uses, *helper_imports(entry.path)]):
            queue.append((sub, dep))

    return Reach(
        effects=tuple(sorted(effects.items())),
        secrets=tuple(sorted(secrets.items())),
        uses=tuple(sorted(parent_of)),
        missing=tuple(sorted(dict.fromkeys(missing))),
        cycle=cycle,
    )


# -- core purity ---------------------------------------------------------


def core_problems(path: Path) -> list[str]:
    """Why a core module is not the pure code the layer requires it to be.

    Core is pure by rule, not convention (§12): the gate can check a helper's
    declared job against its declared side effects only because the body is the
    sole place its reach comes from, and the moment shared code can do more,
    that check is worth whatever the shared code is allowed to do. Tier 0/1
    only — pure computation, at most reading a path it was handed. No network,
    no subprocess, no dynamic execution. (No secrets is enforced elsewhere, for
    free: the store requires a `codeact_helper_*` caller, and core modules load
    under `codeact_core_*`.)
    """
    from . import guard

    try:
        text = path.read_text()
    except OSError as exc:
        return [f"core/{path.stem}: unreadable ({exc})"]

    problems: list[str] = []
    for finding in guard.scan(text):
        if finding.tier >= 3 or (
            finding.tier == 2 and guard.capability_for(finding) != "filesystem"
        ):
            problems.append(
                f"core/{path.stem} is not pure: {finding.describe()} — shared "
                "code that touches the world is already a helper, so package it "
                "as one"
            )
    for layer, dep in scan(path).edges:
        if layer != "core":
            problems.append(
                f"core/{path.stem} imports the {dep!r} helper — core may import "
                "the standard library and core only, or its reach would launder "
                "through every helper that uses it"
            )
    if scan(path).helpers:
        names = ", ".join(scan(path).helpers)
        problems.append(
            f"core/{path.stem} declares @helper ({names}) — a core module with "
            "a card is a helper with extra steps; move it to the helpers layer"
        )
    return problems


def closure_hash(path: Path) -> str:
    """One hash over the file and everything it imports, transitively."""
    digest = hashlib.sha256()
    for name, source in closure(path):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(source.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]
