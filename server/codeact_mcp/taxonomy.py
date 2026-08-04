"""The job and domain vocabularies.

Closed on purpose. Free-form tag sets rot within a few dozen entries; adding a
new job category should be a deliberate act, not a side effect of writing a
helper. Note that the job axis and the side-effect axis largely agree — the two
world-touching jobs are exactly the two that need privileged capabilities — so a
declared job that contradicts declared side effects is a misfiled helper.
"""

from __future__ import annotations

JOBS: dict[str, str] = {
    "acquire": "bring data in from outside the process",
    "parse": "turn serialized or unstructured input into structure",
    "transform": "structured to structured: reshape, filter, join, aggregate",
    "inspect": "answer a question about something without changing it",
    "present": "format existing data for reading",
    "generate": "produce a new artifact: code, text, config",
    "mutate": "change external state: write, commit, POST",
    "orchestrate": "sequence, retry, or parallelize other helpers",
}

# Jobs that inherently reach outside the process.
WORLD_TOUCHING = frozenset({"acquire", "mutate"})

# Jobs that must be pure. `orchestrate` is deliberately absent from both sets —
# it inherits whatever the helpers it calls require.
PURE = frozenset({"parse", "transform", "inspect", "present", "generate"})

DOMAINS: dict[str, str] = {
    "ast": "Python source and syntax trees",
    "data": "tabular or record-shaped data",
    "db": "databases and query results",
    "fs": "files and directories",
    "git": "version control history and state",
    "github": "the GitHub API and its objects",
    "http": "web requests and responses",
    "shell": "processes and command execution",
    "text": "strings, encodings, and documents",
    "time": "dates, durations, and scheduling",
}

SIDE_EFFECTS: dict[str, str] = {
    "none": "pure computation over its arguments",
    "filesystem": "reads or writes the filesystem",
    "network": "makes network calls",
    "process": "spawns processes",
}

# Which side effects each job may plausibly declare. A `parse` helper that opens
# a socket is either mislabeled or badly designed; either way a human should see
# it before it is approved.
_ALLOWED_EFFECTS: dict[str, frozenset[str]] = {
    "acquire": frozenset({"filesystem", "network", "process"}),
    "mutate": frozenset({"filesystem", "network", "process"}),
    "orchestrate": frozenset(SIDE_EFFECTS),
    # Pure jobs may read the filesystem (parsing a file is still parsing) but
    # must not reach the network or spawn processes.
    "parse": frozenset({"none", "filesystem"}),
    "transform": frozenset({"none"}),
    "inspect": frozenset({"none", "filesystem"}),
    "present": frozenset({"none"}),
    "generate": frozenset({"none", "filesystem"}),
}


def job_allows(job: str, side_effects: str) -> bool:
    return side_effects in _ALLOWED_EFFECTS.get(job, frozenset())


def job_index() -> str:
    """The always-in-context tier: categories and definitions, no helpers.

    Cheap enough to inject every session, which is what lets the agent know the
    shape of the library before it knows anything in it.
    """
    return "\n".join(f"  {name:<12} {desc}" for name, desc in JOBS.items())
