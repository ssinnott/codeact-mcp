#!/usr/bin/env python3
"""PreToolUse(Bash): route governed commands through CodeAct.

Reads the hook payload on stdin and answers with a permission decision. What
counts as governed lives in `~/.codeact/config.json` under `commands`, and the
policy itself lives in `codeact_mcp.commands` so that the interpreter's spawn
check and this hook can never drift apart.

Every failure path here exits 0 and allows the command. A policy that ships
off by default has no business breaking `Bash` because its own config was
malformed — and a refusal the human never asked for is worse than a miss.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def decide() -> dict | None:
    from codeact_mcp import commands, config

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None

    if payload.get("tool_name") != "Bash":
        return None
    line = (payload.get("tool_input") or {}).get("command") or ""
    if not line.strip():
        return None

    policy = commands.Policy.load(config.load())
    if not policy.active:
        return None

    decision = policy.check_line(
        line, surface=commands.BASH, resolve=commands.resolve_kube_context
    )
    commands.record(decision, line, surface=commands.BASH, mode=policy.mode)

    if decision.allowed or policy.mode == "audit":
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask" if policy.mode == "ask" else "deny",
            "permissionDecisionReason": decision.message,
        }
    }


def main() -> int:
    try:
        output = decide()
    except Exception:
        return 0
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
