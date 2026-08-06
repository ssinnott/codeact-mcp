"""In-interpreter discovery.

The MCP tools are the front door, but CodeAct's premise is that composing things
in code is what helps — so discovery has to work from inside the code channel
too, without a round trip out to a tool call.
"""

from __future__ import annotations

from typing import Any


class Helpers:
    """Bound as `helpers` in the interpreter namespace."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def __repr__(self) -> str:
        counts = self._registry.counts_by_job()
        if not counts:
            return "<no helpers available>"
        shape = " · ".join(f"{job} ({n})" for job, n in sorted(counts.items()))
        return f"<{sum(counts.values())} helpers: {shape}> — helpers.search('...') to look"

    def search(self, task: str = "", job: str = "", domain: str = "", limit: int = 15) -> str:
        from .search import search

        found = search(
            self._registry.available(),
            task=task,
            jobs=[job] if job else (),
            domains=[domain] if domain else (),
            limit=limit,
        )
        if not found:
            return "nothing matched"
        return "\n".join(e.card.index_line() for e in found)

    def card(self, name: str) -> str:
        entry = self._registry.get(name)
        if entry is not None:
            return entry.card.render()
        reason = self._registry.broken_reason(name)
        if reason:
            return f"{name} failed to load, so it is not callable: {reason}"
        return f"no helper named {name!r}"

    def by_job(self, job: str) -> str:
        return self.search(job=job)

    def jobs(self) -> str:
        from .taxonomy import JOBS

        counts = self._registry.counts_by_job()
        return "\n".join(f"  {name:<12} {counts.get(name, 0):>3}  {desc}" for name, desc in JOBS.items())

    def names(self) -> list[str]:
        return sorted(e.name for e in self._registry.available())
