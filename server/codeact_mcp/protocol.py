"""Minimal MCP server over JSON-RPC on stdio.

Hand-rolled against the MCP spec rather than taking the SDK as a dependency, so
the plugin runs anywhere a bare `python3` exists with no install step. Only the
handful of methods a tools-only server needs are implemented.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC error codes we actually use.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


# JSON Schema type names, mapped to what actually arrives out of json.loads.
# bool is excluded from the numerics deliberately: `True` is an int in Python
# and nowhere else, and a client that sent one meant something else.
_TYPES: dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _type_error(name: str, value: Any, want: str) -> str | None:
    expected = _TYPES.get(want)
    if expected is None:
        return None
    if want in ("number", "integer") and isinstance(value, bool):
        return f"{name!r} must be a {want}, got a boolean"
    if isinstance(value, expected):
        return None
    return f"{name!r} must be a {want}, got {type(value).__name__}"


def validate(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Check a tool call's arguments against the schema the tool published.

    Without this the arguments go straight into `handler(**arguments)`, and the
    only thing standing between a client and the tool is Python's own calling
    convention. That fails in two unhelpful ways. A misspelled or extra key
    surfaces as a raw `TypeError: propose_helper() got an unexpected keyword
    argument`, which names neither the tool's real arguments nor the typo. Worse,
    a wrong *type* does not fail at all: `jobs="parse"` where an array was
    declared is a perfectly good keyword argument, and the string is then
    iterated one character at a time, so the call silently returns nothing
    instead of saying what was wrong.

    Every problem is reported at once, so a caller fixes one call rather than
    discovering the next defect on the next round trip.
    """
    properties = schema.get("properties") or {}
    problems = [
        f"missing required argument {name!r}"
        for name in schema.get("required") or ()
        if name not in arguments
    ]
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            accepted = ", ".join(sorted(properties)) or "no arguments"
            problems.append(f"unknown argument {name!r} — accepted: {accepted}")
            continue
        problem = _type_error(name, value, spec.get("type", ""))
        if problem:
            problems.append(problem)
            continue
        # Enums are how the taxonomy reaches the client; a value outside one is
        # a category the library cannot have anything filed under.
        choices = spec.get("enum") or (spec.get("items") or {}).get("enum")
        if choices and spec.get("type") == "array" and isinstance(value, list):
            bad = [v for v in value if v not in choices]
            if bad:
                problems.append(
                    f"{name!r} has no such value(s) {', '.join(map(repr, bad))} — "
                    f"choose from: {', '.join(map(str, choices))}"
                )
        elif choices and spec.get("type") != "array" and value not in choices:
            problems.append(
                f"{name!r} must be one of: {', '.join(map(str, choices))}, got {value!r}"
            )
    return problems


class Server:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._tools: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def tool(self, name: str, description: str, schema: dict[str, Any], **meta: Any):
        """Register a tool. `meta` becomes the tool's `_meta` block."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            entry: dict[str, Any] = {
                "name": name,
                "description": description,
                "inputSchema": schema,
            }
            if meta:
                entry["_meta"] = meta
            self._tools.append(entry)
            self._handlers[name] = fn
            self._schemas[name] = schema
            return fn

        return register

    # -- dispatch ---------------------------------------------------------

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        handler = self._handlers.get(name)
        if handler is None:
            return {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            }
        arguments = params.get("arguments") or {}
        problems = validate(self._schemas.get(name) or {}, arguments)
        if problems:
            return {
                "content": [
                    {"type": "text", "text": "invalid arguments:\n  " + "\n  ".join(problems)}
                ],
                "isError": True,
            }
        try:
            text = handler(**arguments)
        except Exception as exc:  # surfaced to the model, not raised at the client
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": text}]}

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            # Echo the client's protocol version when we can speak it.
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        if method == "tools/list":
            return {"tools": self._tools}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "ping":
            return {}
        raise LookupError(method)

    # -- loop -------------------------------------------------------------

    def run(self) -> None:
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")

            # Notifications carry no id and take no response.
            if msg_id is None:
                continue

            try:
                result = self._handle(method, msg.get("params") or {})
                response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
            except LookupError:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method: {method}"},
                }
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": INTERNAL_ERROR, "message": str(exc)},
                }

            out.write(json.dumps(response) + "\n")
            out.flush()
