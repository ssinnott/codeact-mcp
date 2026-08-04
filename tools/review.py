#!/usr/bin/env python3
"""The review app: approve or reject pending helpers in a browser.

    python3 tools/review.py

Starts a local server on 127.0.0.1 with a random token in the URL and prints the
link. Deliberately a separate process from the MCP server — reviewing is your
activity, it should not need an active Claude session, and the MCP server is
stdio-bound and per-session anyway.

Stdlib only, one HTML file, no build step.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import miner, proposals, registry, trial  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
PAGE = (Path(__file__).parent / "review.html").read_text()


def state() -> dict:
    pending = proposals.listing(proposals.PENDING)
    rejected = [p for p in proposals.listing(proposals.REJECTED) if not p.reason]
    reg = registry.registry(reload=True)
    queues = miner.queues(reg)
    return {
        "pending": [_proposal_json(p) for p in pending],
        "failed": [_proposal_json(p) for p in rejected],
        "helpers": [
            {
                "name": e.name,
                "job": e.card.meta.job,
                "domains": list(e.card.meta.domains),
                "effects": e.card.meta.side_effects,
                "quarantine": e.card.quarantine,
                "builtin": e.builtin,
                "summary": e.card.prose.summary,
            }
            for e in sorted(reg.entries.values(), key=lambda e: e.name)
        ],
        "queues": queues,
    }


def _proposal_json(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status,
        "revises": p.revises,
        "job": p.job,
        "domains": p.domains,
        "effects": p.side_effects,
        "secrets": p.requires_secrets,
        "problems": p.problems,
        "card": p.card,
        "source": p.source,
        "escalates": bool(p.diff.get("escalates")),
        "diff": {k: v for k, v in p.diff.items() if k != "escalates"},
        "examples": p.examples,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # quiet
        pass

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest((query.get("t") or [""])[0], TOKEN)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload).encode(), "application/json", status)

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send(b"forbidden", "text/plain", 403)
        route = urlparse(self.path).path
        if route == "/":
            return self._send(PAGE.encode(), "text/html; charset=utf-8")
        if route == "/api/state":
            return self._json(state())
        self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send(b"forbidden", "text/plain", 403)
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "message": "bad request"}, 400)

        if route == "/api/approve":
            ok, message = proposals.approve(body.get("id", ""))
            return self._json({"ok": ok, "message": message})
        if route == "/api/reject":
            ok, message = proposals.reject(body.get("id", ""), body.get("reason", ""))
            return self._json({"ok": ok, "message": message})
        if route == "/api/trial":
            proposal = proposals.load(body.get("id", ""))
            if proposal is None:
                return self._json({"ok": False, "message": "no such proposal"}, 404)
            examples = [
                {"code": e["code"], "note": e.get("note", ""), "setup": e.get("setup", "")}
                for e in proposal.examples
            ] or [{"code": f"{proposal.name}"}]
            report = trial.run(proposal.source, examples)
            report["effect_lines"] = trial.summarize_effects(report.get("effects") or {})
            return self._json({"ok": not report.get("fatal"), "report": report})
        self._send(b"not found", "text/plain", 404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/?t={TOKEN}"
    print(f"codeact review  →  {url}\nCtrl-C to stop.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
