"""User-settable behaviour, in ~/.codeact/config.json.

Defaults are chosen so a fresh install is useful and honest: the guard audits
rather than blocks, because its tier boundaries are guesses until real usage
corrects them, and a wall with no door is just a broken tool.
"""

from __future__ import annotations

import json
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    # audit | enforce — see §9. Enforcement only makes sense once there is a way
    # to say yes to what it blocks, which is what propose_helper provides.
    "guard": "audit",
    # Capabilities granted for every session without asking. Empty by default;
    # the point of the guard is that reaching outside gets reviewed.
    "granted": [],
    # Seconds a single run_python call may take before it is interrupted.
    "timeout": 30,
}


def load() -> dict[str, Any]:
    path = paths.root() / "config.json"
    data = dict(DEFAULTS)
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
