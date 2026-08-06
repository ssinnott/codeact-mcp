"""The library as an import surface: `from codeact.helpers import group_by`.

Names resolve at import time against the user's library first and the shipped
seeds second — the same shadowing rule the registry uses, so an edge lands on
the same file the name would have resolved to in a session.

There is deliberately nothing to enumerate here. A helper is reached by name or
not at all, which keeps `from codeact.helpers import *` from existing and keeps
every edge visible in the line that creates it.
"""

from __future__ import annotations

from typing import Any

from codeact_mcp import linkage


def __getattr__(name: str) -> Any:
    # Dunder lookups are the import machinery asking whether this is a package;
    # answering them with a library search would turn every `from` statement
    # into a scan of the whole directory.
    if name.startswith("_"):
        raise AttributeError(name)
    return linkage.load_helper(name)


def __dir__() -> list[str]:
    return linkage.helper_names()
