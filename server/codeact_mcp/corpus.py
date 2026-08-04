"""Append-only log of everything executed, across every project.

Nothing reads this yet. It starts recording in phase 1 anyway, because the miner
that eventually consumes it needs history and history cannot be backfilled.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

from . import paths

SESSION_ID = uuid.uuid4().hex[:12]

# Redact things that look like credentials before they reach disk. The miner
# fingerprints structure, not literals, so nothing downstream needs the real
# values. Conservative by design: a false positive costs nothing.
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # Long opaque literals assigned to a suggestively named variable.
    re.compile(
        r"(?i)\b(token|secret|password|passwd|api_?key|auth|credential)\s*=\s*"
        r"[\"'][^\"']{12,}[\"']"
    ),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def record(
    code: str,
    *,
    outcome: str,
    duration_ms: int,
    guard_findings: list | None = None,
    error_type: str | None = None,
) -> None:
    """Best effort — logging must never take a working session down."""
    try:
        paths.ensure()
        entry = {
            "ts": time.time(),
            "session": SESSION_ID,
            "project": os.getcwd(),
            "code": redact(code),
            "outcome": outcome,
            "error_type": error_type,
            "duration_ms": duration_ms,
            "guard": [
                {"tier": f.tier, "kind": f.kind, "name": f.name}
                for f in (guard_findings or [])
            ],
        }
        with paths.corpus_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
