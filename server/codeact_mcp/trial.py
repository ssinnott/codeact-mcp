"""Trial runs for the review app.

Running a pending candidate is the single most dangerous operation in the
system: it is unreviewed code, and the human is most inclined to execute it
without thinking precisely because they are trying to decide whether to trust
it. So a trial run is deliberately more constrained than a normal session —
scratch directory, no real credentials, and resource limits — and it reports
**what the code touched**, not only what it returned.

The side-effect report is the point. Output alone tells you a candidate is
plausible; it takes "files written, hosts contacted, processes spawned" to tell
you it does nothing else, and that second half is what actually needs a human.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
MARKER = "\x00CODEACT-TRIAL\x00"
DEFAULT_TIMEOUT = 20.0

# sys.addaudithook gives a real record of what ran rather than a guess. These
# are the events worth surfacing to a reviewer.
_RUNNER = r'''
import contextlib, io, json, os, resource, sys, traceback

resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))

effects = {"files_read": set(), "files_written": set(), "network": set(),
           "processes": set(), "imports": set()}
WATCH_IMPORTS = {"socket", "ssl", "subprocess", "ctypes", "pickle", "urllib",
                 "http", "requests", "httpx", "multiprocessing"}

# Import machinery reads hundreds of files. Surfacing those would bury the
# handful a reviewer actually needs to see.
import sysconfig
_NOISE = tuple(
    p for p in (
        __SERVER__,
        sysconfig.get_paths().get("stdlib"),
        sysconfig.get_paths().get("purelib"),
        sysconfig.get_paths().get("platlib"),
    ) if p
)

def _boring(path):
    return (
        "__pycache__" in path
        or path.endswith((".pyc", ".pyi"))
        or path.startswith(_NOISE)
    )

def _hook(event, args):
    try:
        if event == "open":
            target = args[0]
            if not isinstance(target, str):
                return  # an already-open fd, not a path worth naming
            # builtins.open passes a mode string; os.open passes flags with
            # mode None. Missing the second case reports a written file as a
            # read, which is precisely backwards in a report about reach.
            mode = args[1] or ""
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = any(c in mode for c in "wxa+") or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)
            )
            if _boring(target):
                return  # our own import machinery, on either side
            effects["files_written" if writing else "files_read"].add(target)
        elif event in ("socket.connect", "socket.getaddrinfo"):
            effects["network"].add(str(args[1] if event == "socket.connect" else args[0]))
        elif event in ("subprocess.Popen", "os.system", "os.exec"):
            effects["processes"].add(str(args[0])[:200])
        elif event == "import" and args[0] in WATCH_IMPORTS:
            effects["imports"].add(args[0])
    except Exception:
        pass

sys.path.insert(0, __SERVER__)
import importlib.util

spec = importlib.util.spec_from_file_location("codeact_trial_target", __PATH__)
module = importlib.util.module_from_spec(spec)
sys.addaudithook(_hook)
try:
    spec.loader.exec_module(module)
except BaseException:
    print(__MARKER__ + json.dumps({
        "results": [], "effects": {},
        "fatal": traceback.format_exc(limit=3)}))
    raise SystemExit(0)

ns = dict(vars(module))
results = []
for example in __EXAMPLES__:
    buf = io.StringIO()
    entry = {"code": example["code"], "note": example.get("note", "")}
    wants_error = example.get("raises", False)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if example.get("setup"):
                exec(example["setup"], ns)
            try:
                compiled = compile(example["code"], "<trial>", "eval")
                is_expr = True
            except SyntaxError:
                compiled = compile(example["code"], "<trial>", "exec")
                is_expr = False
            if is_expr:
                value = eval(compiled, ns)
                if value is not None:
                    print(repr(value))
            else:
                exec(compiled, ns)
        entry["output"] = buf.getvalue().strip()
        entry["ok"] = not wants_error
    except BaseException:
        entry["output"] = (buf.getvalue()
                           + "".join(traceback.format_exception_only(*sys.exc_info()[:2]))).strip()
        entry["ok"] = wants_error
    results.append(entry)

print(__MARKER__ + json.dumps({
    "results": results,
    "effects": {k: sorted(v)[:40] for k, v in effects.items() if v},
    "fatal": "",
}))
'''


def run(source: str, examples: list[dict], timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Execute a candidate's examples in a scratch directory, under audit.

    Nothing here reaches the real project: the source is written to a fresh temp
    tree and the process runs with its cwd there, so a helper that writes
    relative paths cannot touch anything that matters.
    """
    scratch = Path(tempfile.mkdtemp(prefix="codeact-trial-"))
    target = scratch / "candidate.py"
    target.write_text(source)

    script = (
        _RUNNER.replace("__SERVER__", repr(str(SERVER_DIR)))
        .replace("__PATH__", repr(str(target)))
        .replace("__EXAMPLES__", repr(examples))
        .replace("__MARKER__", repr(MARKER))
    )

    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(scratch),
            # A trial never gets real credentials. Making that the default is
            # what stops "let me just test it" from being the exfiltration path.
            env={"PATH": "/usr/bin:/bin", "HOME": str(scratch), "CODEACT_TRIAL": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"results": [], "effects": {}, "fatal": f"timed out after {timeout:.0f}s"}

    _, marker, payload = proc.stdout.rpartition(MARKER)
    if not marker:
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        return {"results": [], "effects": {}, "fatal": detail or "no output from trial"}
    try:
        report = json.loads(payload.strip())
    except json.JSONDecodeError:
        return {"results": [], "effects": {}, "fatal": "trial produced unreadable output"}

    report["scratch"] = str(scratch)
    return report


def summarize_effects(effects: dict) -> list[str]:
    """One line per kind of reach, ordered by how much a reviewer should care."""
    order = [
        ("network", "contacted"),
        ("processes", "spawned"),
        ("files_written", "wrote"),
        ("imports", "imported"),
        ("files_read", "read"),
    ]
    lines = []
    for key, verb in order:
        items = effects.get(key) or []
        if not items:
            continue
        shown = ", ".join(items[:6])
        more = f" (+{len(items) - 6} more)" if len(items) > 6 else ""
        lines.append(f"{verb}: {shown}{more}")
    return lines or ["no observable side effects"]
