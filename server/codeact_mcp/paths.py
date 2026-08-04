"""Locations of the on-disk library. One library per machine, in the home dir."""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """The codeact home. Override with CODEACT_HOME (used by the tests)."""
    env = os.environ.get("CODEACT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codeact"


def helpers_dir() -> Path:
    return root() / "helpers"


def proposals_dir() -> Path:
    return root() / "proposals"


def corpus_path() -> Path:
    return root() / "corpus.jsonl"


def ensure() -> Path:
    """Create the tree if absent. Safe to call on every startup."""
    r = root()
    for d in (r, helpers_dir(), proposals_dir()):
        d.mkdir(parents=True, exist_ok=True)
    gitignore = r / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("corpus.jsonl\nfingerprints.db\n*.log\n")
    return r
