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

# `core` joins this in §12 step 3; the resolution and hashing below are already
# written in terms of the layer, so it plugs in without a second mechanism.
LAYER_MODULES = {"codeact.helpers": "helpers"}


def layer_dirs(layer: str) -> tuple[Path, ...]:
    """Where a layer's files live, most specific first.

    User library before shipped seeds, so resolving an edge picks the same file
    the registry would have registered under that name.
    """
    if layer == "helpers":
        return (paths.helpers_dir(), paths.seeds_dir())
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


# -- loading -------------------------------------------------------------

# Names currently being executed, so a cycle is refused with the path that
# caused it instead of exhausting the stack. Cycles are also refused at the
# gate (§12), but the loader is what a hand-edited library hits first.
_loading: list[str] = []


def load_module(path: Path):
    """Execute a helper file under its canonical module name.

    The `codeact_helper_` prefix is load-bearing beyond tidiness: the secrets
    store identifies its caller by walking out to the first frame whose module
    carries it, so a module named anything else cannot read a secret.
    """
    name = f"codeact_helper_{path.stem}"
    if name in _loading:
        cycle = " -> ".join(m.removeprefix("codeact_helper_") for m in [*_loading, name])
        raise ImportError(f"import cycle among helpers: {cycle}")

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


def closure_hash(path: Path) -> str:
    """One hash over the file and everything it imports, transitively."""
    digest = hashlib.sha256()
    for name, source in closure(path):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(source.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]
