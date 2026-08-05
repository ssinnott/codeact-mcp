"""Locations of the on-disk library. One library per machine, in the home dir."""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """The codeact home. Override with CODEACT_HOME (used by the tests)."""
    env = os.environ.get("CODEACT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codeact"


def cli() -> Path:
    """The `codeact` CLI shipped alongside the server.

    Spelled out in full wherever a message suggests running it: the plugin
    installs under a hashed directory, so `codeact secret set X` is only
    actionable for someone who has already aliased it.
    """
    return Path(__file__).resolve().parents[2] / "codeact"


def helpers_dir() -> Path:
    return root() / "helpers"


def proposals_dir() -> Path:
    return root() / "proposals"


def corpus_path() -> Path:
    return root() / "corpus.jsonl"


def traces_dir() -> Path:
    """One transcript per interpreter session — see trace.py."""
    return root() / "traces"


IGNORED = ("corpus.jsonl", "fingerprints.db", "*.log", "traces/")


def ensure() -> Path:
    """Create the tree if absent. Safe to call on every startup."""
    r = root()
    for d in (r, helpers_dir(), proposals_dir(), traces_dir()):
        d.mkdir(parents=True, exist_ok=True)
    gitignore = r / ".gitignore"
    # Rewritten when an entry is missing, not only when the file is absent:
    # traces hold session output, and an install that predates them would
    # otherwise keep an ignore file that never learns to exclude it.
    try:
        current = gitignore.read_text().splitlines() if gitignore.exists() else []
        missing = [line for line in IGNORED if line not in current]
        if missing:
            gitignore.write_text("\n".join(current + missing) + "\n")
    except OSError:
        pass
    return r
