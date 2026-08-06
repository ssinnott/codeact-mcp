"""Discovering, loading and quarantining helpers.

Two sources: helpers shipped with the plugin (read-only seeds, so discovery has
something to find on day one) and helpers in the user's library at
`~/.codeact/helpers`. The user's library wins on a name collision.

Each helper is `<name>.py` (authored) plus an optional `<name>.json` sidecar
(generated: captured example output, and the hashes it was captured from).
Keeping generated artifacts out of the authored file is what makes the diff a
human reviews contain only what a human wrote.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import cards, linkage, paths
from .cards import Card
from .helper import meta_of

_SOURCE_CHANGED = (
    "the source changed since its examples were last verified, so the "
    "documented behaviour may no longer be true"
)
_DEPENDENCY_CHANGED = (
    "code it depends on changed since its examples were last verified, so the "
    "documented behaviour may no longer be true"
)


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Entry:
    card: Card
    fn: Callable
    path: Path
    builtin: bool

    @property
    def name(self) -> str:
        return self.card.name

    @property
    def quarantined(self) -> bool:
        return bool(self.card.quarantine)


@dataclass
class Broken:
    """A helper that exists on disk but whose file would not load.

    Named, and kept, because "no such helper" and "that helper is broken" are
    different facts and only the second one tells the agent what to do about it
    (§12). Before this the reason was printed by the CLI and nowhere else, so
    from a session the helper had simply never existed.
    """

    name: str
    path: Path
    builtin: bool
    reason: str


def _sidecar(path: Path) -> dict:
    side = path.with_suffix(".json")
    if not side.exists():
        return {}
    try:
        return json.loads(side.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_sidecar(path: Path, captured: list[dict], helper: str = "", **extra) -> None:
    """Record captured example output, keyed by helper.

    A file may define more than one helper, and each has its own examples, so a
    flat list here would let the second capture silently erase the first.
    Existing entries for other helpers in the same file are preserved.
    """
    side = path.with_suffix(".json")
    existing = _sidecar(path)
    helpers = dict(existing.get("helpers") or {})
    if helper:
        helpers[helper] = captured
    payload = {
        "helpers": helpers,
        "source_hash": source_hash(path.read_text()),
        # Both, and not one: the closure decides whether the examples still
        # hold, and the file's own hash is what tells a stale helper from one
        # whose dependency moved under it.
        "closure_hash": linkage.closure_hash(path),
        **extra,
    }
    side.write_text(json.dumps(payload, indent=2) + "\n")


def _captured_for(side: dict, name: str) -> list[dict]:
    helpers = side.get("helpers")
    if isinstance(helpers, dict):
        return helpers.get(name) or []
    # Legacy flat form, written before sidecars were keyed by helper.
    return side.get("captured") or []


def _staleness(path: Path, text: str, side: dict) -> str:
    """Whether the captured examples still describe the code that runs.

    The closure hash covers the file plus every source it imports, transitively,
    so editing a dependency quarantines its dependents even though no helper
    file changed (§12) — and the two reasons are told apart, because "you edited
    this" and "something under it moved" send a reviewer to different places.

    A sidecar written before edges existed records only `source_hash`. A helper
    from that era could not depend on anything, so its own source *is* its whole
    closure and comparing that stays exactly right.
    """
    own_changed = side.get("source_hash") != source_hash(text)
    recorded = side.get("closure_hash")
    if recorded is None:
        return _SOURCE_CHANGED if own_changed else ""
    if recorded == linkage.closure_hash(path):
        return ""
    return _SOURCE_CHANGED if own_changed else _DEPENDENCY_CHANGED


def _load_file(path: Path, builtin: bool) -> Iterator[Entry]:
    module = linkage.load_module(path)
    side = _sidecar(path)
    text = path.read_text()

    quarantine = ""
    if side:
        quarantine = _staleness(path, text, side) or side.get("quarantine", "")

    for value in vars(module).values():
        if not callable(value) or meta_of(value) is None:
            continue
        if getattr(value, "__module__", None) != module.__name__:
            continue  # imported from another helper, not defined here
        card = cards.build(value, _captured_for(side, value.__name__))
        card.quarantine = quarantine
        yield Entry(card=card, fn=value, path=path, builtin=builtin)


class Registry:
    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}
        self.broken: dict[str, Broken] = {}
        self.errors: list[str] = []

    def load(self) -> "Registry":
        self.entries.clear()
        self.broken.clear()
        self.errors.clear()
        # User helpers load second so they shadow same-named seeds.
        for directory, builtin in ((paths.seeds_dir(), True), (paths.helpers_dir(), False)):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                try:
                    for entry in _load_file(path, builtin):
                        prior = self.entries.get(entry.name)
                        # Shadowing a seed from the user library is intended; two
                        # files in the same scope claiming one name is a mistake,
                        # and silently keeping whichever sorted last would make it
                        # very hard to find.
                        if prior is not None and prior.builtin == builtin:
                            self.errors.append(
                                f"{path.name}: duplicate helper {entry.name!r}, "
                                f"already defined in {prior.path.name}"
                            )
                        self.entries[entry.name] = entry
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    self.errors.append(f"{path.name}: {reason}")
                    # Read the names out of the AST, since the file itself would
                    # not run. A file that does not even parse names nothing,
                    # and only the error above is left to report it.
                    for name in linkage.declared_helpers(path):
                        self.broken[name] = Broken(name, path, builtin, reason)
        return self

    # -- views ------------------------------------------------------------

    def available(self) -> list[Entry]:
        """Everything callable. Quarantined helpers are deliberately excluded.

        The agent trusts cards absolutely and cannot verify them, so a helper
        with known-wrong documentation is worse than no helper at all.
        """
        return [e for e in self.entries.values() if not e.quarantined]

    def get(self, name: str) -> Entry | None:
        return self.entries.get(name)

    def broken_reason(self, name: str) -> str:
        """Why a name that exists on disk isn't callable, if that's the case.

        Resolved at lookup rather than at load, so a working user helper
        shadowing a broken seed of the same name reports nothing: the name
        answers, and which file it came from is the CLI's business.
        """
        entry = self.broken.get(name)
        if entry is None or name in self.entries:
            return ""
        return f"{entry.path.name}: {entry.reason}"

    def counts_by_job(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.available():
            counts[entry.card.meta.job] = counts.get(entry.card.meta.job, 0) + 1
        return counts

    def namespace(self) -> dict[str, Callable]:
        return {e.name: e.fn for e in self.available()}


_registry: Registry | None = None
_stamp: tuple | None = None


def _dir_stamp() -> tuple:
    """Cheap fingerprint of the helper directories.

    An edited dependency lands here too, since a helper's dependencies are
    themselves files in these directories — which is what makes a quarantine
    triggered by a closure change show up without a restart.
    """
    marks = []
    for directory in (paths.seeds_dir(), paths.helpers_dir()):
        try:
            marks.append(
                tuple(sorted((p.name, p.stat().st_mtime_ns) for p in directory.glob("*.py")))
            )
        except OSError:
            marks.append(())
    return tuple(marks)


def registry(reload: bool = False) -> Registry:
    """The loaded library, re-read whenever the helper files change.

    The server is long-lived, so caching forever would mean a helper approved
    mid-session stays invisible until restart — exactly when the agent has just
    been told it now exists.
    """
    global _registry, _stamp
    stamp = _dir_stamp()
    if _registry is None or reload or stamp != _stamp:
        _registry = Registry().load()
        _stamp = stamp
    return _registry
