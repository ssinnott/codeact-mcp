"""Session manager: owns the worker subprocess and the timeout policy."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

WORKER = Path(__file__).with_name("worker.py")

# After a timeout we SIGINT first — that raises KeyboardInterrupt inside the
# worker, which unwinds the running call but keeps the namespace. Only if the
# worker ignores that do we kill it and lose state.
GRACE_AFTER_INTERRUPT = 3.0


class Timeout(Exception):
    pass


class Session:
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self.proc: subprocess.Popen | None = None
        self._seq = 0
        self.restarts = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.cwd,
            text=False,
        )

    def _ensure(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        return self.proc  # type: ignore[return-value]

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()
        self.restarts += 1

    # -- io ---------------------------------------------------------------

    def _read_line(self, deadline: float) -> bytes | None:
        """Read one newline-terminated message, or None if the deadline passes."""
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        fd = proc.stdout.fileno()
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:  # worker died
                return None
            buf.extend(chunk)
            if b"\n" in buf:
                return bytes(buf).split(b"\n", 1)[0]

    def _send(self, payload: dict) -> None:
        proc = self._ensure()
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()

    def request(self, payload: dict, timeout: float) -> dict:
        self._seq += 1
        payload = {**payload, "id": self._seq}
        self._send(payload)

        line = self._read_line(time.monotonic() + timeout)
        if line is not None:
            return json.loads(line)

        # Timed out. Try to interrupt without losing the namespace.
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            line = self._read_line(time.monotonic() + GRACE_AFTER_INTERRUPT)
            if line is not None:
                return json.loads(line)

        self.restart()
        raise Timeout(
            f"no response after {timeout:.0f}s and the interpreter did not respond to "
            "an interrupt, so it was restarted — all session state was lost"
        )

    # -- operations -------------------------------------------------------

    def execute(self, code: str, timeout: float) -> dict:
        return self.request({"op": "exec", "code": code}, timeout)

    def state(self, timeout: float = 10.0) -> dict:
        return self.request({"op": "state"}, timeout)
