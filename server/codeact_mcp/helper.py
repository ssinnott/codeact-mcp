"""The `@helper` decorator: the structured half of a card.

Prose lives in the docstring (parsed by `cards.py`), because prose belongs where
an author will actually maintain it. Everything machine-checkable lives here,
where it can be validated without guessing at English.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True)
class Example:
    """A worked example, executed at approval time.

    Output is never written by the author — the gate runs the code and records
    what actually happened, so documented behaviour is observed rather than
    asserted. Re-running these later is what detects a stale card.

    Set `raises` for an example that demonstrates a failure mode. Showing how a
    helper refuses bad input is often more useful than another success case, and
    without this flag the gate would read the traceback as a broken example.
    """

    code: str
    note: str = ""
    setup: str = ""
    raises: bool = False


@dataclass
class Meta:
    job: str
    domains: tuple[str, ...]
    side_effects: str
    examples: tuple[Example, ...]
    requires_secrets: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    cost: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _coerce_examples(examples: Iterable[Any]) -> tuple[Example, ...]:
    out = []
    for item in examples:
        if isinstance(item, Example):
            out.append(item)
        elif isinstance(item, str):
            out.append(Example(code=item))
        elif isinstance(item, dict):
            out.append(Example(**item))
        else:
            raise TypeError(f"example must be a str, dict or Example, got {type(item).__name__}")
    return tuple(out)


def helper(
    *,
    job: str,
    domains: Sequence[str],
    side_effects: str = "none",
    examples: Sequence[Any] = (),
    requires_secrets: Sequence[str] = (),
    uses: Sequence[str] = (),
    cost: str = "",
    **extra: Any,
) -> Callable[[Callable], Callable]:
    """Mark a function as a library helper and attach its structured metadata.

    Args:
        job: One of the eight job categories. Exactly one.
        domains: One to three domain tags.
        side_effects: none | filesystem | network | process.
        examples: Runnable snippets. Their real output is captured at approval.
        requires_secrets: Secret names this helper is authorized to read.
            Approving the helper approves this specific binding and nothing else.
        uses: Names of other helpers this one calls. The gate verifies the
            declaration against the imports, both directions, and a helper's
            effective reach is its own plus everything it uses, transitively.
        cost: Free-text hint when a call is slow or hits the network.
    """

    def decorate(fn: Callable) -> Callable:
        fn.__codeact__ = Meta(  # type: ignore[attr-defined]
            job=job,
            domains=tuple(domains),
            side_effects=side_effects,
            examples=_coerce_examples(examples),
            requires_secrets=tuple(requires_secrets),
            uses=tuple(uses),
            cost=cost,
            extra=extra,
        )
        return fn

    return decorate


def meta_of(fn: Any) -> Meta | None:
    return getattr(fn, "__codeact__", None)
