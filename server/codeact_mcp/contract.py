"""Running a helper's examples and capturing what actually happened.

Examples do double duty. At approval time their real output is captured into the
card, so documented behaviour is observed rather than asserted — which removes
any possibility of a hallucinated example in a document the agent fully trusts
and cannot check. Afterwards the same examples are contract tests: re-run them,
and a mismatch means the card has gone stale and the helper must be quarantined.

They run in a subprocess so an example that hangs or exits cannot take the
server with it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 30.0

_RUNNER = r"""
import contextlib, io, json, sys, traceback
sys.path.insert(0, {server!r})
import importlib.util

spec = importlib.util.spec_from_file_location("codeact_example_target", {path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ns = dict(vars(module))
results = []
for example in {examples!r}:
    buf = io.StringIO()
    entry = {{"code": example["code"], "note": example.get("note", "")}}
    wants_error = example.get("raises", False)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if example.get("setup"):
                exec(example["setup"], ns)
            # Decide eval-vs-exec by compiling, never by catching SyntaxError
            # from the run: an example that legitimately raises SyntaxError at
            # runtime (compile(), ast.parse()) would otherwise execute twice,
            # and any side effect it had would happen twice with it.
            try:
                compiled = compile(example["code"], "<example>", "eval")
                is_expression = True
            except SyntaxError:
                compiled = compile(example["code"], "<example>", "exec")
                is_expression = False
            if is_expression:
                value = eval(compiled, ns)
                if value is not None:
                    print(repr(value))
            else:
                exec(compiled, ns)
        entry["output"] = buf.getvalue().strip()
        # An example flagged as demonstrating a failure but which succeeded is
        # itself a defect: the documented failure mode no longer happens.
        entry["ok"] = not wants_error
        if wants_error:
            entry["output"] = (entry["output"] + "\n[expected an exception, none raised]").strip()
    except BaseException:
        lines = traceback.format_exception_only(*sys.exc_info()[:2])
        entry["output"] = (buf.getvalue() + "".join(lines)).strip()
        entry["ok"] = wants_error
    results.append(entry)

print("\x00CODEACT\x00" + json.dumps(results))
"""

MARKER = "\x00CODEACT\x00"


def run_examples(path: Path, examples: list[dict], timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Execute each example, returning its code, captured output and success."""
    if not examples:
        return []

    script = _RUNNER.format(server=str(SERVER_DIR), path=str(path), examples=examples)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(path.parent),
        )
    except subprocess.TimeoutExpired:
        return [
            {"code": ex["code"], "note": ex.get("note", ""), "output": "timed out", "ok": False}
            for ex in examples
        ]

    # rpartition, not partition: an example's own prints are captured, but a
    # subprocess it spawns inherits the real stdout and could emit the marker.
    # Splitting on the last occurrence means only the runner's own trailing
    # payload is ever parsed.
    _, marker, payload = proc.stdout.rpartition(MARKER)
    if marker:
        try:
            return json.loads(payload.strip())
        except json.JSONDecodeError:
            marker = ""  # fall through and report it as a runner failure
    if not marker:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        return [
            {
                "code": ex["code"],
                "note": ex.get("note", ""),
                "output": f"example runner failed: {detail}",
                "ok": False,
            }
            for ex in examples
        ]
    return []


# Output that legitimately differs between runs. Without this, any example
# touching a temp file reports drift forever — and a drift check that always
# fires is one nobody reads.
_VOLATILE = [
    re.compile(r"/tmp/[\w./-]+"),
    re.compile(r"[A-Za-z]:\\\\Users\\\\[\w\\\\.-]+"),
    re.compile(r"0x[0-9a-fA-F]{6,}"),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    re.compile(r"\b\d+\.\d{4,}\b"),  # unrounded timings
]


def normalize(text: str) -> str:
    for pattern in _VOLATILE:
        text = pattern.sub("<varies>", text)
    return text.strip()


def compare(captured: list[dict], fresh: list[dict]) -> list[str]:
    """Real differences between a card's recorded output and a fresh run.

    Paired by position, not by code string: two examples may legitimately share
    the same code (different setup), and keying by code would collapse them and
    then report the survivor as drifting against the wrong baseline.
    """
    drift: list[str] = []
    for was, now in zip(captured, fresh):
        if was.get("code") != now.get("code"):
            # The example set itself changed — that is a revision, not drift.
            continue
        if normalize(was.get("output") or "") != normalize(now.get("output") or ""):
            drift.append(
                f"`{now['code']}` documented {was.get('output')!r} "
                f"but now produces {now.get('output')!r}"
            )
    return drift
