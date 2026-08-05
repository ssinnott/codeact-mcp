"""Session manager: owns the worker subprocess and the timeout policy."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config, paths

WORKER = Path(__file__).with_name("worker.py")

# After a timeout we SIGINT first — that raises KeyboardInterrupt inside the
# worker, which unwinds the running call but keeps the namespace. Only if the
# worker ignores that do we kill it and lose state.
GRACE_AFTER_INTERRUPT = 3.0


class Timeout(Exception):
    pass


class SandboxUnavailable(RuntimeError):
    """run_as was configured but could not be honoured."""


def sudoers_line(runner: str) -> str:
    """The rule an unprivileged install needs, scoped as tightly as it can be."""
    import getpass

    return f"{getpass.getuser()} ALL=({runner}) NOPASSWD: {sys.executable}"


def _spawn_plan(runner: str | None) -> tuple[list[str], dict]:
    """How to launch the worker, given the configured user.

    Two routes, because an unprivileged process cannot drop to another user on
    its own — `subprocess(user=)` raises PermissionError without CAP_SETUID.

    * Already root: drop directly. The group must be set too; passing `user`
      alone leaves the gid at root, which is the classic way a "privilege drop"
      turns out to have dropped almost nothing.
    * Otherwise: go through sudo, which needs a NOPASSWD rule. `-n` so a
      missing rule fails immediately instead of blocking on a password prompt
      nobody will ever see. sudo relays SIGINT, so the timeout escalation still
      interrupts the worker rather than losing its namespace.
    """
    base = [sys.executable, "-u", str(WORKER)]
    if not runner:
        return base, {}

    if os.getuid() == 0:
        import grp
        import pwd

        record = pwd.getpwnam(runner)
        groups = [g.gr_gid for g in grp.getgrall() if runner in g.gr_mem]
        return base, {"user": record.pw_uid, "group": record.pw_gid, "extra_groups": groups}

    return ["sudo", "-n", "-u", runner, *base], {}


class Session:
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self.proc: subprocess.Popen | None = None
        self._stderr = None
        self._seq = 0
        self.restarts = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        runner = config.get("run_as")
        # A file rather than a pipe: nothing reads the worker's stderr during
        # normal operation, and a full pipe buffer would wedge the interpreter.
        self._stderr = tempfile.TemporaryFile()
        try:
            # Inside the guard: resolving an unknown user raises KeyError, and
            # that deserves the same actionable message as any other failure to
            # honour run_as.
            command, extra = _spawn_plan(runner)
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                cwd=self.cwd,
                text=False,
                **extra,
            )
        except Exception as exc:
            # Never fall back to running with full privileges. Someone who
            # configured run_as believes they are isolated, and silently
            # ignoring it would be worse than not offering the option at all.
            raise SandboxUnavailable(
                f"cannot start the interpreter as {config.get('run_as')!r}: "
                f"{type(exc).__name__}: {exc}\n"
                f"Run `python3 {paths.cli()} sandbox` to check the setup, or clear "
                f"`run_as` in ~/.codeact/config.json to run as yourself."
            ) from exc

        if runner:
            self._verify_sandbox(runner)

    def _verify_sandbox(self, runner: str) -> None:
        """Confirm the worker actually came up as the other user.

        sudo failing prints to stderr and exits non-zero rather than raising, so
        without this a missing sudoers rule would look like a mysterious timeout
        instead of a setup problem with a known fix.
        """
        assert self.proc is not None
        for _ in range(40):
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        else:
            return  # still alive after 2s, so it started

        self._stderr.seek(0)
        detail = self._stderr.read().decode("utf-8", "replace").strip()[-400:]
        raise SandboxUnavailable(
            f"the interpreter could not start as {runner!r}.\n{detail}\n\n"
            f"Grant it with this line in /etc/sudoers.d/codeact:\n"
            f"  {sudoers_line(runner)}\n"
            f"Then check with `python3 {paths.cli()} sandbox`."
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
