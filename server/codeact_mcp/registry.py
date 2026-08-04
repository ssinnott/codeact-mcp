"""Discovering, loading and quarantining helpers.

Two sources: helpers shipped with the plugin (read-only seeds, so discovery has
something to find on day one) and helpers in the user's library at
`~/.codeact/helpers`. The user's library wins on a name collision.

Each helper is `<name>.py` (authored) plus an optional `<name>.json` sidecar
(generated: captured example output, and the source hash it was captured from).
Keeping generated artifacts out of the authored file is what makes the diff a
human reviews contain only what a human wrote.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import cards, paths
from .cards import Card
from .helper import meta_of

SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"


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


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"codeact_helper_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
        **extra,
    }
    side.write_text(json.dumps(payload, indent=2) + "\n")


def _captured_for(side: dict, name: str) -> list[dict]:
    helpers = side.get("helpers")
    if isinstance(helpers, dict):
        return helpers.get(name) or []
    # Legacy flat form, written before sidecars were keyed by helper.
    return side.get("captured") or []


def _load_file(path: Path, builtin: bool) -> Iterator[Entry]:
    module = _load_module(path)
    side = _sidecar(path)
    text = path.read_text()

    quarantine = ""
    if side:
        if side.get("source_hash") != source_hash(text):
            quarantine = (
                "the source changed since its examples were last verified, so the "
                "documented behaviour may no longer be true"
            )
        elif side.get("quarantine"):
            quarantine = side["quarantine"]

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
        self.errors: list[str] = []

    def load(self) -> "Registry":
        self.entries.clear()
        self.errors.clear()
        # User helpers load second so they shadow same-named seeds.
        for directory, builtin in ((SEEDS_DIR, True), (paths.helpers_dir(), False)):
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
                    self.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
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
    """Cheap fingerprint of the helper directories."""
    marks = []
    for directory in (SEEDS_DIR, paths.helpers_dir()):
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
