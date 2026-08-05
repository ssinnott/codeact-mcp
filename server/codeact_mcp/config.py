"""User-settable behaviour, in ~/.codeact/config.json.

Defaults are chosen so a fresh install is useful and honest: the guard audits
rather than blocks, because its tier boundaries are guesses until real usage
corrects them, and a wall with no door is just a broken tool.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from . import commands, paths

DEFAULTS: dict[str, Any] = {
    # audit | enforce — see §9. Enforcement only makes sense once there is a way
    # to say yes to what it blocks, which is what propose_helper provides.
    "guard": "audit",
    # Capabilities granted for every session without asking. Empty by default;
    # the point of the guard is that reaching outside gets reviewed.
    "granted": [],
    # Seconds a single run_python call may take before it is interrupted.
    "timeout": 30,
    # Keep a per-session transcript of everything the interpreter ran and
    # returned, for reading after the fact with `codeact trace`. On by default:
    # the reason to want one is almost always a session that has already gone
    # wrong, and by then it is too late to switch it on.
    "trace": True,
    # Run the interpreter as a different OS user. This is the one real
    # containment boundary in the system — the AST guard is policy, but a
    # different uid is enforced by the kernel. Null means "same user as Claude
    # Code", which is what Bash already grants.
    "run_as": None,
    # Shell commands routed through CodeAct instead of Bash — see §9, layer 0.
    # Ships off: these rules are a template, and blocking a command someone
    # relies on without being asked is worse than blocking nothing. Flip the
    # mode with `codeact policy enforce`.
    "commands": {"mode": "off", "rules": list(commands.DEFAULT_RULES)},
}


def load() -> dict[str, Any]:
    path = paths.root() / "config.json"
    # Deep, not shallow: `commands` is a nested dict, and a caller editing it —
    # which is exactly what `codeact policy` does — would otherwise rewrite the
    # defaults for the rest of the process.
    data = copy.deepcopy(DEFAULTS)
    try:
        data.update(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    return data


def save(values: dict[str, Any]) -> None:
    paths.ensure()
    (paths.root() / "config.json").write_text(json.dumps(values, indent=2) + "\n")


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def enforcing() -> bool:
    return str(get("guard")).lower() == "enforce"
