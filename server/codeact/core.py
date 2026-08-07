"""Shared code as an import surface: `from codeact.core import records`.

Core is the second layer of §12: pure code that helper authors — who read
source — may share, with no card and no way to be discovered. If the agent
should be able to find it and call it, it is a helper; if only helper code
should call it, it is core. A name here resolves to the *module*, against the
user's `~/.codeact/core/` first and the shipped `seeds/core/` second — the same
shadowing rule as everywhere else.

There is deliberately nothing to enumerate here, for the same reason as in
`codeact.helpers`: an edge exists as the line that creates it or not at all.
"""

from __future__ import annotations

from typing import Any

from codeact_mcp import linkage


def __getattr__(name: str) -> Any:
    if name.startswith("_"):
        raise AttributeError(name)
    return linkage.load_core(name)


def __dir__() -> list[str]:
    return linkage.core_names()
