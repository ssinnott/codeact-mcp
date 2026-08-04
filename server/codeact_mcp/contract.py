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
            try:
                value = eval(example["code"], ns)
                if value is not None:
                    print(repr(value))
            except SyntaxError:
                exec(example["code"], ns)
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

    _, marker, payload = proc.stdout.partition(MARKER)
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
    return json.loads(payload.strip())


def compare(captured: list[dict], fresh: list[dict]) -> list[str]:
    """Differences between the card's recorded output and a fresh run."""
    drift: list[str] = []
    by_code = {entry["code"]: entry for entry in captured}
    for entry in fresh:
        was = by_code.get(entry["code"])
        if was is None:
            continue
        if (was.get("output") or "") != (entry.get("output") or ""):
            drift.append(
                f"`{entry['code']}` documented {was.get('output')!r} "
                f"but now produces {entry.get('output')!r}"
            )
    return drift
