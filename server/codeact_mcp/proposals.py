"""Proposing helpers, and the gate a proposal must pass before a human sees it.

`propose_helper` installs nothing. It writes a pending proposal and runs every
check that can be automated, so the human review is spent on judgement rather
than on catching missing type hints.

A proposal that fails the gate is still recorded, with its problems, so the
agent gets one complete list of what to fix instead of a round trip per defect.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import cards, contract, paths, registry, taxonomy

PENDING = "pending"
REJECTED = "rejected"
APPROVED = "approved"


@dataclass
class Proposal:
    id: str
    name: str
    source: str
    status: str
    created: float
    revises: str = ""
    problems: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    card: str = ""
    job: str = ""
    domains: list[str] = field(default_factory=list)
    side_effects: str = "none"
    requires_secrets: list[str] = field(default_factory=list)
    reason: str = ""
    diff: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return paths.proposals_dir() / f"{self.id}.json"

    def save(self) -> "Proposal":
        paths.ensure()
        self.path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        return self


def _from_json(text: str) -> Proposal | None:
    """Rebuild a proposal from its file, tolerating fields we do not know.

    `Proposal(**data)` is too strict for stored data: a file written by another
    version of the dataclass — one field added, one renamed — raises TypeError,
    and the proposal then vanishes from every listing and every `show`. Silently
    dropping a pending review is the worst possible failure here, so unknown
    fields are ignored and absent ones fall back to their defaults. A record
    without an id has no way to be acted on, and only that is refused.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not data.get("id"):
        return None
    known = {f.name for f in fields(Proposal)}
    kept = {k: v for k, v in data.items() if k in known}
    kept.setdefault("name", kept["id"])
    kept.setdefault("source", "")
    kept.setdefault("status", PENDING)
    kept.setdefault("created", 0.0)
    return Proposal(**kept)


def load(proposal_id: str) -> Proposal | None:
    path = paths.proposals_dir() / f"{proposal_id}.json"
    try:
        return _from_json(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def listing(status: str = "") -> list[Proposal]:
    out = []
    for path in sorted(paths.proposals_dir().glob("*.json")):
        try:
            proposal = _from_json(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if proposal is None:
            continue
        if not status or proposal.status == status:
            out.append(proposal)
    out.sort(key=lambda p: p.created, reverse=True)
    return out


def _load_candidate(source: str, name: str):
    """Import candidate source in a scratch file and find its decorated function."""
    tmp = Path(tempfile.mkdtemp(prefix="codeact-proposal-")) / f"{name}.py"
    tmp.write_text(source)
    entries = list(registry._load_file(tmp, builtin=False))
    return tmp, entries


def _capability_delta(old: Any, new_meta) -> dict[str, Any]:
    """What a revision changes about reach.

    A helper quietly gaining network access or a secret is the single most
    dangerous edit in the system, and it looks identical whether the cause is
    malice or a confused agent — so it is surfaced on its own rather than left
    to be spotted in a source diff.
    """
    if old is None:
        return {}
    before, after = old.card.meta, new_meta
    delta: dict[str, Any] = {}
    if before.side_effects != after.side_effects:
        delta["side_effects"] = [before.side_effects, after.side_effects]
    gained = set(after.requires_secrets) - set(before.requires_secrets)
    if gained:
        delta["secrets_gained"] = sorted(gained)
    if before.job != after.job:
        delta["job"] = [before.job, after.job]
    delta["escalates"] = bool(
        gained
        or (before.side_effects == "none" and after.side_effects != "none")
    )
    return delta


def _settle(proposal: Proposal) -> Proposal:
    """Record the proposal under the status its problems imply.

    Every exit from the gate goes through here. The early exits — source that
    will not import, source defining no matching helper — used to return before
    the status was decided, so a proposal that had already failed was filed as
    `pending`: it sat in the reviewer's queue claiming to await a decision, with
    an empty card because nothing ever got far enough to render one, and it was
    missing from the failed queue where its problems would have explained it.
    """
    if proposal.problems:
        proposal.status = REJECTED
    return proposal.save()


def propose(name: str, source: str, revises: str = "") -> Proposal:
    """Run the full gate and record a pending proposal. Installs nothing."""
    proposal = Proposal(
        id=uuid.uuid4().hex[:10],
        name=name,
        source=source,
        status=PENDING,
        created=time.time(),
        revises=revises,
    )

    reg = registry.registry()
    if not name.isidentifier():
        proposal.problems.append(f"{name!r} is not a valid Python identifier")
    existing = reg.get(name)
    if existing is not None and not revises:
        proposal.problems.append(
            f"a helper named {name!r} already exists — pass revises={name!r} to change it"
        )
    if revises and reg.get(revises) is None:
        proposal.problems.append(f"revises={revises!r} but no such helper exists")

    try:
        tmp, entries = _load_candidate(source, name)
    except Exception as exc:
        proposal.problems.append(f"source does not import: {type(exc).__name__}: {exc}")
        return _settle(proposal)

    match = [e for e in entries if e.name == name]
    if not match:
        found = ", ".join(e.name for e in entries) or "nothing"
        proposal.problems.append(
            f"source defines no @helper named {name!r} (found: {found})"
        )
        return _settle(proposal)
    if len(entries) > 1:
        proposal.problems.append(
            "define exactly one helper per proposal — "
            f"this source defines {', '.join(e.name for e in entries)}"
        )

    entry = match[0]
    meta = entry.card.meta
    proposal.problems += cards.validate(entry.fn)
    proposal.job = meta.job
    proposal.domains = list(meta.domains)
    proposal.side_effects = meta.side_effects
    proposal.requires_secrets = list(meta.requires_secrets)

    # Near-duplicate check: a helper that already exists should be extended,
    # not shadowed by a second one that does almost the same thing.
    if not revises:
        similar = _similar_to(entry, reg)
        if similar:
            proposal.problems.append(
                f"looks close to the existing helper {similar!r} — consider "
                f"revises={similar!r} instead of a second helper"
            )

    results = contract.run_examples(tmp, [
        {"code": ex.code, "note": ex.note, "setup": ex.setup, "raises": ex.raises}
        for ex in meta.examples
    ])
    proposal.examples = results
    for bad in [r for r in results if not r["ok"]]:
        proposal.problems.append(
            f"example failed: {bad['code']} -> {(bad['output'] or '').splitlines()[-1:] or ['']}"[:300]
        )

    entry.card.captured = results
    proposal.card = entry.card.render()

    if revises:
        proposal.diff = _capability_delta(reg.get(revises), meta)

    return _settle(proposal)


def _similar_to(entry, reg) -> str:
    """Cheap overlap check against the existing library."""
    words = set(entry.card.search_text().split())
    for other in reg.available():
        if other.name == entry.name:
            continue
        if other.card.meta.job != entry.card.meta.job:
            continue
        theirs = set(other.card.search_text().split())
        overlap = len(words & theirs) / max(len(words | theirs), 1)
        if overlap > 0.6:
            return other.name
    return ""


def approve(proposal_id: str) -> tuple[bool, str]:
    """Install a pending proposal into the library.

    This is the only path from proposed to callable, and it is never reachable
    by the agent — see the MCP tool's requiresUserInteraction flag and the
    review app.
    """
    proposal = load(proposal_id)
    if proposal is None:
        return False, f"no proposal {proposal_id!r}"
    if proposal.status == APPROVED:
        return False, f"{proposal.name} was already approved"
    if proposal.problems:
        return False, (
            f"{proposal.name} did not pass the gate:\n  "
            + "\n  ".join(proposal.problems)
        )

    paths.ensure()
    target = paths.helpers_dir() / f"{proposal.name}.py"
    target.write_text(proposal.source)
    registry.write_sidecar(target, proposal.examples, helper=proposal.name)

    proposal.status = APPROVED
    proposal.save()
    registry.registry(reload=True)
    verb = "revised" if proposal.revises else "added"
    return True, f"{verb} {proposal.name} -> {target}"


def reject(proposal_id: str, reason: str = "") -> tuple[bool, str]:
    proposal = load(proposal_id)
    if proposal is None:
        return False, f"no proposal {proposal_id!r}"
    proposal.status = REJECTED
    proposal.reason = reason
    proposal.save()
    return True, f"rejected {proposal.name}"


def vocabulary_hint() -> str:
    jobs = ", ".join(taxonomy.JOBS)
    domains = ", ".join(taxonomy.DOMAINS)
    return f"jobs: {jobs}\ndomains: {domains}"
