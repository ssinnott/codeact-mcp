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


class Server:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._tools: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = {}

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
        try:
            text = handler(**(params.get("arguments") or {}))
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
