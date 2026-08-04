#!/usr/bin/env python3
"""Score card sufficiency: can a helper be used correctly from its card alone?

Because the card is the entire interface the agent gets, its quality is directly
testable — hand someone only the card, give them a task, and see whether the code
they write runs. A self-reported "the card was fine" is not evidence, so every
submission is executed against the real helper here.

    python3 tools/eval_cards.py evals/cards.json

Input is a JSON list of {helper, code, sufficient, missing}.
"""

from __future__ import annotations

import io
import json
import sys
import contextlib
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import registry  # noqa: E402

# The tasks refer to variables that would exist in a real session. Each helper's
# fixture supplies them, so the submitted code runs in the context it assumed.
FIXTURES: dict[str, str] = {
    "parse_key_values": "raw = '# comment\\nexport A=1\\nB=\"two words\"\\n\\nC=3\\n'",
    "extract_code_blocks": (
        "doc = '''intro\\n```python\\nprint(1)\\n```\\ntext\\n```js\\nvar a=1\\n```\\n"
        "```python\\nprint(2)\\n```\\n'''"
    ),
    "parse_duration": "",
    "group_by": (
        "rows = [{'team': 'a', 'n': 1}, {'team': 'b', 'n': 2}, {'team': 'a', 'n': 3}]"
    ),
    "summarize_numeric": "latencies = [12.0, 7.5, 30.25, 9.0, 44.5, 3.25, 18.0]",
    "diff_records": (
        "old = [{'id': 1, 'v': 'a'}, {'id': 2, 'v': 'b'}]\n"
        "new = [{'id': 2, 'v': 'B'}, {'id': 3, 'v': 'c'}]"
    ),
    "find_files": (
        "import os, tempfile\n"
        "_root = tempfile.mkdtemp()\n"
        "os.makedirs(os.path.join(_root, 'src', '__pycache__'), exist_ok=True)\n"
        "open(os.path.join(_root, 'src', 'a.py'), 'w').write('x = 1')\n"
        "open(os.path.join(_root, 'src', 'b.txt'), 'w').write('t')\n"
        "open(os.path.join(_root, 'src', '__pycache__', 'c.py'), 'w').write('y = 2')\n"
        "os.chdir(_root)\n"
    ),
    "python_symbols": (
        "import os, tempfile\n"
        "_root = tempfile.mkdtemp()\n"
        "open(os.path.join(_root, 'example.py'), 'w')"
        ".write('def alpha():\\n    pass\\n\\nclass Beta:\\n    pass\\n')\n"
        "os.chdir(_root)\n"
    ),
    "text_stats": "doc = 'one two three\\n\\nfour five\\nsix\\n'",
    "to_markdown_table": (
        "rows = [{'name': 'a', 'score': 1, 'extra': 'x'}, {'name': 'b', 'score': 2}]"
    ),
    "write_jsonl": "records = [{'id': 1}, {'id': 2}, {'id': 3}]",
    "retry_call": (
        "_calls = {'n': 0}\n"
        "def flaky_fetch():\n"
        "    _calls['n'] += 1\n"
        "    if _calls['n'] < 3:\n"
        "        raise ConnectionError('boom')\n"
        "    return {'ok': True}\n"
    ),
}


def run_case(case: dict, namespace: dict) -> tuple[bool, str]:
    ns = dict(namespace)
    buf = io.StringIO()
    fixture = FIXTURES.get(case["helper"], "")
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if fixture:
                exec(fixture, ns)
            exec(case["code"], ns)
    except BaseException:
        return False, traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
    return True, buf.getvalue().strip()[:160]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    cases = json.loads(Path(sys.argv[1]).read_text())
    reg = registry.Registry().load()
    namespace = reg.namespace()

    cwd = Path.cwd()
    ran = claimed = 0
    rows = []
    for case in cases:
        ok, detail = run_case(case, namespace)
        import os

        os.chdir(cwd)  # fixtures may chdir
        ran += ok
        claimed += bool(case.get("sufficient"))
        rows.append((case["helper"], ok, bool(case.get("sufficient")), case.get("missing") or [], detail))

    total = len(cases)
    print(f"{'run':>4} {'said':>5}  helper")
    print("-" * 78)
    for helper, ok, said, missing, detail in sorted(rows):
        print(f"{'ok' if ok else 'FAIL':>4} {'yes' if said else 'no':>5}  {helper}")
        if not ok:
            print(f"                {detail}")
    print("-" * 78)
    print(f"code runs from the card alone   {ran}/{total}  ({ran / total:.0%})")
    print(f"reader judged the card enough   {claimed}/{total}  ({claimed / total:.0%})")

    gaps = [(h, m) for h, _, _, m, _ in rows if m]
    if gaps:
        print(f"\nGaps reported by readers ({sum(len(m) for _, m in gaps)} across {len(gaps)} cards):")
        for helper, missing in sorted(gaps):
            for item in missing:
                print(f"  {helper}: {item}")

    # A card that produced working code but left the reader guessing still has a
    # defect — the next reader may guess differently.
    silent = [h for h, ok, said, m, _ in rows if ok and not said]
    if silent:
        print(f"\nRan but the reader was not confident: {', '.join(sorted(silent))}")

    return 0 if ran == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
