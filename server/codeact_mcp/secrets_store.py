"""Secrets: the agent never sees one, an approved helper may use the ones it declared.

This is §10's capability delegation applied to data rather than syscalls.
Approving a helper approves its access to the specific secrets it declared, so
authorization is a `(secret, helper)` pair — `GITHUB_TOKEN` being reachable by
`fetch_pr_diff` says nothing about anything else.

Three layers, because no single one is sufficient:

1. Never bound in the namespace. `get()` is callable only from an approved
   helper's module, checked by walking the calling frame back to its definition.
   Agent code runs under `exec` with a distinguishable module identity, so this
   is a real check — with the same honest caveat as the AST guard: it stops
   accidental and casual access and is not the security boundary.
2. An opaque `Secret` wrapper whose repr, str and format all redact. Aimed at
   the dominant real failure, which is not exfiltration but accident: a
   credential landing in a log line, an f-string, or a traceback.
3. Egress redaction of everything leaving the interpreter. Tracebacks are the
   critical case — an exception inside an HTTP library will happily print the
   auth header it was called with, and that path bypasses layer 2 entirely.

## On storage

At-rest encryption needs a cipher, and the standard library has none. Rolling
one here would be worse than useless: it would read as protection while
providing none. So the store is a 0600 file, and `describe_storage()` says
exactly that. A keyring backend is the upgrade path, not something to fake.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from . import paths


class Secret:
    """An opaque handle. Every stringification redacts."""

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, value: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_value", value)

    @property
    def name(self) -> str:
        return self._name

    def reveal(self) -> str:
        """The plaintext. Deliberately noisy to write, so it is easy to grep for."""
        return self._value

    def __str__(self) -> str:
        return f"<secret:{self._name}>"

    __repr__ = __str__

    def __format__(self, _spec: str) -> str:
        return str(self)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("codeact-secret", self._name))


def store_path() -> Path:
    """Outside ~/.codeact, so nothing that syncs or commits that tree carries it."""
    override = os.environ.get("CODEACT_SECRETS")
    return Path(override) if override else Path.home() / ".codeact-secrets.json"


def describe_storage() -> str:
    return (
        f"{store_path()} — filesystem permissions only (0600). The standard "
        "library ships no cipher, and pretending otherwise would be worse than "
        "saying so. Use an OS keyring backend for anything you would mind losing."
    )


def _read() -> dict[str, str]:
    try:
        return json.loads(store_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write(values: dict[str, str]) -> None:
    path = store_path()
    path.write_text(json.dumps(values, indent=2) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def put(name: str, value: str) -> None:
    values = _read()
    values[name] = value
    _write(values)


def remove(name: str) -> bool:
    values = _read()
    if name not in values:
        return False
    del values[name]
    _write(values)
    return True


def names() -> list[str]:
    return sorted(_read())


def all_values() -> list[str]:
    """Every plaintext, for egress redaction. Never returned to the agent."""
    return [v for v in _read().values() if v]


def redact(text: str) -> str:
    """Strip known secret values from anything on its way to the model.

    A plain substring scan is enough and costs nothing, and it is the layer that
    makes the other two survivable: it catches the traceback that prints an auth
    header regardless of how carefully the wrapper was used.
    """
    if not text:
        return text
    for value in all_values():
        if len(value) >= 6 and value in text:
            text = text.replace(value, "<redacted>")
    return text


class Denied(RuntimeError):
    pass


# The worker's half of the run_as broker (§10). Under run_as the store file is
# 0600 and owned by the human, so the interpreter process cannot read it — the
# right default, and one that would otherwise break every approved
# secret-using helper. The worker installs a callable here that asks the
# server process over the protocol pipe; the server, running as the human,
# does the read and its own declared-by-an-approved-helper check before
# answering. None means no broker: the local file is the only route.
broker = None


def broker_read(name: str) -> str | None:
    """The server's half: a raw read for relaying to the worker.

    Only ever called in the MCP server process, which runs as the human and
    can read the store; the caller is responsible for having checked that the
    secret is one an approved helper declared.
    """
    return _read().get(name)


def _calling_helper() -> str:
    """Walk out of this module and report the caller's module name."""
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module != __name__:
            return module
        frame = frame.f_back
    return ""


def get(name: str, *, registry_names: set[str] | None = None) -> Secret:
    """Fetch a secret. Only an approved helper that declared it may.

    `registry_names` is injected by the worker so this module does not import
    the registry (which imports cards, which imports this) — a small awkwardness
    in exchange for no import cycle.
    """
    caller = _calling_helper()
    allowed = registry_names if registry_names is not None else set()
    if not caller.startswith("codeact_helper_") and caller not in allowed:
        raise Denied(
            f"secrets are only readable from an approved helper that declared "
            f"them; {caller or 'this code'} did not. Package the work as a "
            f"helper with requires_secrets=[{name!r}] and have it approved."
        )
    values = _read()
    if name not in values and broker is not None:
        # After the caller check, deliberately: the broker widens who can
        # perform the read, not who may ask for it.
        value = broker(name)
        if value:
            return Secret(name, value)
    if name not in values:
        raise KeyError(
            f"no secret named {name!r} — set one with "
            f"`python3 {paths.cli()} secret set {name}`"
        )
    return Secret(name, values[name])
