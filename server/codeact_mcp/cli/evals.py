"""`codeact eval` — the two things about this design that can actually be scored.

**cards** — can a helper be used correctly from its card alone? The card is the
entire interface the agent gets, so its quality is directly testable: hand
someone only the card, give them a task, and see whether the code they write
runs. A self-reported "the card was fine" is not evidence, so every submission
is executed against the real helper.

**retrieval** — does task-driven discovery find the right helper? The eval
splits the two halves that can each fail on their own: the model classifies a
task into jobs and domains, and the server ranks. Feeding recorded
classifications through the real ranker isolates which half is at fault.

    codeact eval cards [evals/cards.json]
    codeact eval retrieval [evals/retrieval.json]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path

from .. import registry, search

# The card tasks refer to variables that would exist in a real session. Each
# helper's fixture supplies them, so the submitted code runs in the context it
# assumed.
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


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "eval",
        help="score card sufficiency and task-driven discovery",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(metavar="<suite>")
    parser.set_defaults(handler=lambda _a: parser.print_help() or 0)

    carder = sub.add_parser("cards", help="can a helper be used from its card alone?")
    carder.add_argument("file", nargs="?")
    carder.set_defaults(handler=run_cards)

    retriever = sub.add_parser("retrieval", help="does discovery find the right helper?")
    retriever.add_argument("file", nargs="?")
    retriever.set_defaults(handler=run_retrieval)


def _dataset(given: str | None, default_name: str) -> Path:
    from . import root

    return Path(given) if given else root() / "evals" / default_name


def helper_name(raw: str) -> str:
    """Readers sometimes echo the whole signature; take the identifier."""
    return raw.split("(")[0].strip()


def run_case(case: dict, namespace: dict) -> tuple[bool | str, str]:
    ns = dict(namespace)
    buf = io.StringIO()
    fixture = FIXTURES.get(helper_name(case["helper"]), "")
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if fixture:
                exec(fixture, ns)
            exec(case["code"], ns)
    except SyntaxError:
        # The submission never compiled — prose leaked into the code block. That
        # is a defect in the answer's format, not evidence about the card, so it
        # is reported separately rather than scored against the helper.
        return "malformed", traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
    except BaseException:
        return False, traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
    return True, buf.getvalue().strip()[:160]


def run_cards(args: argparse.Namespace) -> int:
    path = _dataset(args.file, "cards.json")
    try:
        cases = json.loads(path.read_text())
    except OSError:
        print(f"no such dataset: {path}", file=sys.stderr)
        return 2

    reg = registry.Registry().load()
    namespace = reg.namespace()

    cwd = Path.cwd()
    ran = claimed = 0
    rows = []
    for case in cases:
        ok, detail = run_case(case, namespace)
        os.chdir(cwd)  # fixtures may chdir
        ran += ok is True
        claimed += bool(case.get("sufficient"))
        rows.append(
            (
                helper_name(case["helper"]),
                ok,
                bool(case.get("sufficient")),
                case.get("missing") or [],
                detail,
            )
        )

    total = len(cases)
    print(f"{'run':>4} {'said':>5}  helper")
    print("-" * 78)
    for helper, ok, said, missing, detail in sorted(rows):
        label = {True: "ok", False: "FAIL", "malformed": "n/a"}[ok]
        print(f"{label:>4} {'yes' if said else 'no':>5}  {helper}")
        if ok is not True:
            print(f"                {detail}")
    print("-" * 78)
    malformed = [h for h, ok, _, _, _ in rows if ok == "malformed"]
    scored = total - len(malformed)
    print(f"code runs from the card alone   {ran}/{scored}  ({ran / scored:.0%} of well-formed)")
    print(f"reader judged the card enough   {claimed}/{total}  ({claimed / total:.0%})")
    if malformed:
        print(f"excluded, submission would not compile: {', '.join(sorted(malformed))}")

    gaps = [(h, m) for h, _, _, m, _ in rows if m]
    if gaps:
        print(f"\nGaps reported by readers ({sum(len(m) for _, m in gaps)} across {len(gaps)} cards):")
        for helper, missing in sorted(gaps):
            for item in missing:
                print(f"  {helper}: {item}")

    # A card that produced working code but left the reader guessing still has a
    # defect — the next reader may guess differently.
    silent = [h for h, ok, said, m, _ in rows if ok is True and not said]
    if silent:
        print(f"\nRan but the reader was not confident: {', '.join(sorted(silent))}")

    return 0 if ran == scored else 1


def rank_of(pool, name: str, **kwargs) -> int | None:
    found = search.search(pool, limit=50, **kwargs)
    for position, entry in enumerate(found, start=1):
        if entry.name == name:
            return position
    return None


def run_retrieval(args: argparse.Namespace) -> int:
    path = _dataset(args.file, "retrieval.json")
    try:
        cases = json.loads(path.read_text())
    except OSError:
        print(f"no such dataset: {path}", file=sys.stderr)
        return 2

    pool = registry.Registry().load().available()
    if not pool:
        print("no helpers loaded")
        return 1

    hit1 = hit3 = found = 0
    reciprocal = 0.0
    rows = []

    for case in cases:
        gold = case["gold"]
        classified = rank_of(
            pool,
            gold,
            task=case.get("query", case["task"]),
            jobs=case.get("jobs") or (),
            domains=case.get("domains") or (),
        )
        # The same lookup without the model's category filter, to show whether
        # classification helped or hurt.
        unfiltered = rank_of(pool, gold, task=case.get("query", case["task"]))

        if classified:
            found += 1
            reciprocal += 1 / classified
            hit1 += classified == 1
            hit3 += classified <= 3

        rows.append((case["task"], gold, classified, unfiltered))

    total = len(cases)
    print(f"{'rank':>5} {'nofilter':>9}  gold / task")
    print("-" * 78)
    for task, gold, classified, unfiltered in rows:
        mark = "  " if classified == 1 else ("~ " if classified and classified <= 3 else "X ")
        print(f"{mark}{str(classified or '-'):>3} {str(unfiltered or '-'):>9}  {gold} / {task[:44]}")

    print("-" * 78)
    print(f"hit@1  {hit1}/{total}  ({hit1 / total:.0%})")
    print(f"hit@3  {hit3}/{total}  ({hit3 / total:.0%})")
    print(f"found  {found}/{total}")
    print(f"MRR    {reciprocal / total:.3f}")

    misfiltered = [r for r in rows if r[2] is None and r[3] is not None]
    if misfiltered:
        print(
            f"\n{len(misfiltered)} case(s) where the model's job/domain filter EXCLUDED the "
            "right helper that plain ranking would have found:"
        )
        for task, gold, _, unfiltered in misfiltered:
            print(f"  {gold} (rank {unfiltered} unfiltered) — {task[:60]}")

    return 0 if hit3 == total else 1
