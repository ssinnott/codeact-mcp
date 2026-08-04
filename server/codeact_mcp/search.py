"""Retrieval: the model classifies, the server filters and ranks.

Deciding that a task is `acquire + transform` over `github` is what the model is
already good at and already positioned to do, so it does the semantic step. This
module does exact set filtering plus lexical scoring, which needs no embedding
infrastructure and stays fast well past a hundred helpers.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

from .registry import Entry

_WORD = re.compile(r"[a-z0-9_]+")

# Words that carry no signal in a task description.
_STOP = frozenset(
    """a an the and or of to in for from with on at by is are be this that it its
    i want need get please help me my we our using use used how do does can
    all any some""".split()
)


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word in _STOP or len(word) < 2:
            continue
        out.append(word)
        # Split snake_case so `read_jsonl` also matches "read" and "jsonl".
        out.extend(part for part in word.split("_") if len(part) > 2 and part != word)
    return out


def _bm25(query: list[str], docs: dict[str, list[str]], k1: float = 1.5, b: float = 0.75):
    if not query:
        return {name: 0.0 for name in docs}
    n = len(docs) or 1
    avg_len = sum(len(d) for d in docs.values()) / n
    df: Counter[str] = Counter()
    for tokens in docs.values():
        for term in set(tokens):
            df[term] += 1

    scores = {}
    for name, tokens in docs.items():
        counts = Counter(tokens)
        length = len(tokens) or 1
        score = 0.0
        for term in query:
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * length / avg_len))
        scores[name] = score
    return scores


def search(
    entries: Iterable[Entry],
    task: str = "",
    jobs: Sequence[str] = (),
    domains: Sequence[str] = (),
    limit: int = 15,
    project: str = "",
    usage: dict[str, dict] | None = None,
) -> list[Entry]:
    """Filter by category, then rank. Category filters are hard, ranking is soft."""
    pool = list(entries)

    # Job is a hard filter: eight coarse categories, and a task's job is
    # something the model gets right.
    if jobs:
        wanted = {j.lower() for j in jobs}
        pool = [e for e in pool if e.card.meta.job in wanted]

    # Domain is deliberately NOT a filter. Cross-cutting helpers have no honest
    # domain — `retry_call` wraps any callable, so it is tagged `time` for its
    # backoff, and a perfectly reasonable "retry this network call" search with
    # domains=[http] would exclude the one right answer. Since the job filter has
    # already cut the pool to roughly an eighth, domain is worth more as a
    # ranking signal than as another gate.
    wanted_d = {d.lower() for d in domains} if domains else set()

    if not pool:
        return []

    docs = {e.name: _tokens(e.card.search_text()) for e in pool}
    scores = _bm25(_tokens(task), docs)

    usage = usage or {}
    ranked = []
    for entry in pool:
        score = scores.get(entry.name, 0.0)
        stats = usage.get(entry.name, {})

        if wanted_d:
            overlap = len(wanted_d & set(entry.card.meta.domains))
            # Large enough to dominate ordering when it matches, small enough
            # that a strong lexical match still surfaces without it.
            score += 2.0 * min(overlap, 2)

        # Telemetry priors. Small next to lexical match — they break ties and
        # push known-good helpers up, they don't override relevance.
        calls = stats.get("calls", 0)
        if calls:
            score += 0.15 * math.log1p(calls)
            failures = stats.get("failures", 0)
            score -= 0.5 * (failures / calls)

        # Project affinity: a helper only ever used in one project ranks low
        # elsewhere, since the library is machine-wide and one codebase's
        # single-purpose helper shouldn't clutter every other codebase.
        seen_in = set(stats.get("projects", ()))
        if seen_in:
            if len(seen_in) > 1:
                score += 0.2
            elif project and project not in seen_in:
                score -= 0.3

        ranked.append((score, entry))

    # Stable, and alphabetical when nothing distinguishes two helpers.
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [entry for _, entry in ranked[:limit]]
