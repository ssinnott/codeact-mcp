"""Cards: parse, validate, render.

The agent never reads a helper's source — it reads the card. That makes an
incomplete card not a style nit but a missing interface, so validation here is
deliberately strict, and it is the strictest check in the gate.

Prose format is Google-style docstring sections:

    One-line summary.

    Use when: ...
    Don't use when: ...

    Args:
        name: description
    Returns:
        shape, not just type
    Raises:
        ExcType: when and what to do
    Preconditions:
        ...
"""

from __future__ import annotations

import inspect
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable

from . import taxonomy
from .helper import Meta, meta_of

SECTION_RE = re.compile(
    r"^(Use when|Don't use when|Do not use when|Args|Arguments|Params|Parameters|"
    r"Returns|Return|Raises|Preconditions|Notes)\s*:\s*(.*)$",
    re.IGNORECASE,
)
ITEM_RE = re.compile(r"^(\*?\*?[A-Za-z_][\w.]*)\s*(?:\([^)]*\))?\s*:\s*(.*)$")

_CANON = {
    "arguments": "args",
    "params": "args",
    "parameters": "args",
    "return": "returns",
    "do not use when": "dont_use_when",
    "don't use when": "dont_use_when",
    "use when": "use_when",
}


@dataclass
class Prose:
    summary: str = ""
    use_when: str = ""
    dont_use_when: str = ""
    args: dict[str, str] = field(default_factory=dict)
    returns: str = ""
    raises: dict[str, str] = field(default_factory=dict)
    preconditions: str = ""
    notes: str = ""


def parse_docstring(doc: str | None) -> Prose:
    prose = Prose()
    if not doc:
        return prose

    lines = inspect.cleandoc(doc).splitlines()
    if not lines:
        return prose

    def section_at(line: str):
        """A section header only counts at the left margin.

        cleandoc puts section headers at column 0 and their entries below,
        indented. Matching a stripped line instead would read a parameter
        genuinely named `params`, `notes` or `return` as a new section and
        silently drop every argument documented after it.
        """
        if line[:1].isspace():
            return None
        return SECTION_RE.match(line)

    # Summary runs until the first blank line or the first section header.
    summary: list[str] = []
    idx = 0
    for idx, line in enumerate(lines):
        if not line.strip() or section_at(line):
            break
        summary.append(line.strip())
    else:
        idx = len(lines)
    prose.summary = " ".join(summary).strip()

    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        if current in ("args", "raises"):
            # Deliberately not stripped: _split_items needs the original
            # indentation to tell a new entry from a wrapped line.
            target = prose.args if current == "args" else prose.raises
            for key, value in _split_items("\n".join(buffer)):
                # A helper can raise the same exception type for more than one
                # reason. Overwriting would silently drop every condition but
                # the last, in the section whose whole job is enumerating them.
                target[key] = f"{target[key]} Also: {value}" if key in target else value
        elif "\n".join(buffer).strip():
            setattr(prose, current, " ".join("\n".join(buffer).split()))

    for line in lines[idx:]:
        match = section_at(line)
        if match:
            flush()
            label = match.group(1).lower()
            current = _CANON.get(label, label)
            buffer = [match.group(2)] if match.group(2).strip() else []
        elif current is not None:
            buffer.append(line)
    flush()
    return prose


def _split_items(block: str) -> list[tuple[str, str]]:
    """Parse a `name: description` list where descriptions may wrap.

    Entries sit at the block's base indentation and continuation lines are
    indented further, so relative indentation is what separates them — which is
    why the caller must not strip the block first.
    """
    lines = [line for line in textwrap.dedent(block).splitlines() if line.strip()]
    items: list[tuple[str, str]] = []
    for raw in lines:
        indented = raw[:1].isspace()
        match = ITEM_RE.match(raw.strip())
        if match and (not indented or not items):
            items.append((match.group(1).lstrip("*"), match.group(2).strip()))
        elif items:
            items[-1] = (items[-1][0], f"{items[-1][1]} {raw.strip()}".strip())
    return items


@dataclass
class Card:
    name: str
    signature: str
    meta: Meta
    prose: Prose
    captured: list[dict[str, str]] = field(default_factory=list)
    quarantine: str = ""
    # Effective reach (linkage.Reach), filled in once the whole library is
    # loaded: what this helper can do through everything it uses, and where
    # each capability came from. None until linked; the card then falls back
    # to rendering only what the helper itself declares.
    reach: Any = None

    # -- the two views the agent gets ------------------------------------

    def index_line(self) -> str:
        """One line for the catalog. Cheap enough to list many."""
        domains = ",".join(self.meta.domains)
        return f"{self.name}{self.signature} — {self.prose.summary} [{self.meta.job}/{domains}]"

    def render(self) -> str:
        """The full contract. This is the entire interface the agent gets."""
        p = self.prose
        out = [f"{self.name}{self.signature}", "", p.summary]

        if self.quarantine:
            out += ["", f"!! QUARANTINED: {self.quarantine}", "Do not rely on this helper."]

        if p.use_when:
            out += ["", f"Use when: {p.use_when}"]
        if p.dont_use_when:
            out += [f"Don't use when: {p.dont_use_when}"]

        if p.args:
            out += ["", "Parameters:"]
            out += [f"  {k}: {v}" for k, v in p.args.items()]
        if p.returns:
            out += ["", f"Returns: {p.returns}"]
        if p.raises:
            out += ["", "Raises:"]
            out += [f"  {k}: {v}" for k, v in p.raises.items()]
        if p.preconditions:
            out += ["", f"Preconditions: {p.preconditions}"]

        facts = [f"job: {self.meta.job}", f"domains: {', '.join(self.meta.domains)}"]
        effects = self.effective_effects()
        if effects:
            facts.append(
                "side effects: "
                + "; ".join(e if not via else f"{e} (via {via})" for e, via in effects)
            )
        uses_line = self._uses_line()
        if uses_line:
            facts.append(uses_line)
        secrets = self.effective_secrets()
        if secrets:
            facts.append(
                "requires secrets: "
                + ", ".join(s if not via else f"{s} (via {via})" for s, via in secrets)
            )
        if self.meta.cost:
            facts.append(f"cost: {self.meta.cost}")
        out += ["", " | ".join(facts)]

        # An unset secret is an unmet precondition, said here rather than left
        # to surface as an opaque failure at call time. Only a human can fix
        # it, so the card names the command a human would run.
        unmet = self.unmet_secrets()
        if unmet:
            from . import paths

            out += [""]
            for name in unmet:
                out += [
                    f"!! UNMET PRECONDITION: secret {name} is not set, so calls "
                    f"will fail until a human runs `python3 {paths.cli()} secret "
                    f"set {name}`."
                ]

        if self.captured:
            out += ["", "Examples (output captured from real runs):"]
            for ex in self.captured:
                out.append(f"  >>> {ex['code']}")
                for line in (ex.get("output") or "").splitlines():
                    out.append(f"  {line}")
                if ex.get("note"):
                    out.append(f"  # {ex['note']}")
        elif self.meta.examples:
            out += ["", "Examples (not yet verified):"]
            out += [f"  >>> {ex.code}" for ex in self.meta.examples]

        if p.notes:
            out += ["", f"Notes: {p.notes}"]
        return "\n".join(out)

    # -- effective reach, with declared-only fallback ---------------------

    def effective_effects(self) -> tuple:
        if self.reach is not None and self.reach.effects:
            return self.reach.effects
        if self.meta.side_effects != "none":
            return ((self.meta.side_effects, ""),)
        return ()

    def effective_secrets(self) -> tuple:
        if self.reach is not None and self.reach.secrets:
            return self.reach.secrets
        return tuple((s, "") for s in self.meta.requires_secrets)

    def unmet_secrets(self) -> list[str]:
        """Secrets this helper (or anything it uses) needs that nobody has set.

        Best effort: an unreadable store must never take a card down with it,
        so on any failure the answer is simply "nothing known to be unmet".
        """
        needed = [s for s, _ in self.effective_secrets()]
        if not needed:
            return []
        try:
            from . import secrets_store

            available = set(secrets_store.names())
        except Exception:
            return []
        return [s for s in needed if s not in available]

    def _uses_line(self) -> str:
        direct = self.meta.uses
        closure = self.reach.uses if self.reach is not None else ()
        extra = tuple(n for n in closure if n not in direct)
        if direct and extra:
            return f"uses: {', '.join(direct)} (closure: {', '.join(extra)})"
        if direct:
            return f"uses: {', '.join(direct)}"
        if closure:
            # Imported but never declared — legal before uses= existed, and
            # rendered honestly rather than hidden.
            return f"uses (undeclared): {', '.join(closure)}"
        return ""

    def search_text(self) -> str:
        p = self.prose
        return " ".join(
            [self.name, p.summary, p.use_when, p.returns, self.meta.job, *self.meta.domains]
        ).lower()


def _signature(fn: Callable) -> str:
    """Render a signature with real type names.

    `from __future__ import annotations` turns annotations into strings, which
    would otherwise render as `path: 'str'` in every card.
    """
    try:
        return str(inspect.signature(fn, eval_str=True))
    except Exception:
        try:
            return str(inspect.signature(fn)).replace("'", "")
        except (TypeError, ValueError):
            return "(...)"


def build(fn: Callable, captured: list[dict[str, str]] | None = None) -> Card:
    meta = meta_of(fn)
    if meta is None:
        raise ValueError(f"{getattr(fn, '__name__', fn)} is not decorated with @helper")
    return Card(
        name=fn.__name__,
        signature=_signature(fn),
        meta=meta,
        prose=parse_docstring(fn.__doc__),
        captured=list(captured or []),
    )


# -- validation ----------------------------------------------------------

# Summaries that restate the signature instead of explaining the contract.
_LOW_CONTENT = re.compile(r"^(a |an |the )?(function|helper|method|utility)( that| to| for)?\b", re.I)


def validate(fn: Callable, reach: Any = None) -> list[str]:
    """Every problem with a candidate, not just the first.

    Returning one at a time would cost a round trip per problem, which turns a
    five-defect proposal into five exchanges.

    Pass `reach` (linkage.Reach) to check the job against the helper's
    *effective* side effects rather than only its own — a transform that calls
    an acquire is not a transform, however pure its own body looks (§12).
    """
    problems: list[str] = []
    meta = meta_of(fn)
    if meta is None:
        return ["not decorated with @helper(job=..., domains=[...])"]

    name = getattr(fn, "__name__", "<anonymous>")
    problems += _validate_taxonomy(meta, name)
    problems += _validate_signature(fn)
    problems += _validate_prose(fn, parse_docstring(fn.__doc__))
    if reach is not None:
        problems += _validate_reach(meta, name, reach)

    if not meta.examples:
        problems.append(
            "no examples: at least one is required, and its real output is captured "
            "into the card at approval time"
        )
    return problems


def _validate_taxonomy(meta: Meta, name: str) -> list[str]:
    problems = []
    if meta.job not in taxonomy.JOBS:
        problems.append(
            f"unknown job {meta.job!r}; must be one of {', '.join(sorted(taxonomy.JOBS))}"
        )
    if not meta.domains:
        problems.append("no domains declared; one to three are required")
    elif len(meta.domains) > 3:
        problems.append(f"{len(meta.domains)} domains declared; at most 3 keeps the index usable")
    for domain in meta.domains:
        if domain not in taxonomy.DOMAINS:
            problems.append(
                f"unknown domain {domain!r}; must be one of {', '.join(sorted(taxonomy.DOMAINS))}"
            )
    if meta.side_effects not in taxonomy.SIDE_EFFECTS:
        problems.append(f"unknown side_effects {meta.side_effects!r}")
    elif meta.job in taxonomy.JOBS and not taxonomy.job_allows(meta.job, meta.side_effects):
        problems.append(
            f"job {meta.job!r} is inconsistent with side_effects {meta.side_effects!r} — "
            f"{name} is probably misfiled, or does more than its job claims"
        )
    if meta.requires_secrets and meta.side_effects == "none":
        problems.append("declares secrets but no side effects; a pure function has nothing to authenticate to")
    return problems


def _validate_reach(meta: Meta, name: str, reach: Any) -> list[str]:
    """The job checked against what the helper can reach, not what it declares.

    Only inherited effects are re-checked here — the helper's own declaration
    is _validate_taxonomy's job, and reporting it twice would say the same
    thing in two voices.
    """
    problems = []
    for effect, via in reach.effects:
        if not via:
            continue
        if meta.job in taxonomy.JOBS and not taxonomy.job_allows(meta.job, effect):
            problems.append(
                f"job {meta.job!r} is inconsistent with side effect {effect!r} "
                f"reached via {via!r} — a {meta.job} that calls into {via} "
                "inherits its reach, so either the job is wrong or the "
                "dependency is"
            )
    for missing in reach.missing:
        problems.append(
            f"uses {missing!r}, but no helper by that name exists in the library"
        )
    if reach.cycle:
        problems.append(
            "dependency cycle: " + " -> ".join(reach.cycle) + " — a helper "
            "cannot use itself, directly or through anything it uses"
        )
    return problems


def _validate_signature(fn: Callable) -> list[str]:
    problems = []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ["signature could not be inspected"]

    for param in sig.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.annotation is inspect.Parameter.empty:
            problems.append(f"parameter {param.name!r} has no type annotation")
    if sig.return_annotation is inspect.Signature.empty:
        problems.append("no return type annotation")
    return problems


def _validate_prose(fn: Callable, prose: Prose) -> list[str]:
    problems = []
    if not prose.summary:
        return ["no docstring: the card is the entire interface the agent gets"]

    if _LOW_CONTENT.match(prose.summary) or len(prose.summary.split()) < 4:
        problems.append(
            f"summary {prose.summary!r} restates the signature instead of explaining "
            "what the helper is for"
        )
    if not prose.use_when:
        problems.append("no 'Use when:' section — when to reach for this is the whole point")
    if not prose.dont_use_when:
        problems.append(
            "no \"Don't use when:\" section — the boundary is what stops the helper "
            "being used where it doesn't belong, and it is the half most often omitted"
        )

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return problems

    documented = set(prose.args)
    for param in sig.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.name not in documented:
            problems.append(f"parameter {param.name!r} is not documented in Args:")
    for extra in documented - {p.name for p in sig.parameters.values()}:
        problems.append(f"Args: documents {extra!r}, which is not a parameter")

    returns_something = sig.return_annotation not in (None, "None", inspect.Signature.empty)
    if returns_something and not prose.returns:
        problems.append("no 'Returns:' section describing the shape of the result")
    elif returns_something and len(prose.returns.split()) < 3:
        problems.append(
            f"'Returns: {prose.returns}' describes a type, not a shape — say what is "
            "in it (`list of {sha, author, date}`, not `list[dict]`)"
        )
    return problems
