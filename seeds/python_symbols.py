"""Listing the top-level functions and classes a Python file defines."""

from __future__ import annotations

import ast
from pathlib import Path

from codeact import helper

_KINDS: dict[type, str] = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async function",
    ast.ClassDef: "class",
}


@helper(
    job="inspect",
    domains=["ast", "fs"],
    side_effects="filesystem",
    examples=[
        {
            "setup": (
                "import pathlib, tempfile\n"
                "_dir = pathlib.Path(tempfile.mkdtemp())\n"
                "_mod = _dir / 'sample.py'\n"
                "_lines = [\n"
                "    \"'Module docstring.'\",\n"
                "    'import os',\n"
                "    '',\n"
                "    \"VERSION = '1.0'\",\n"
                "    '',\n"
                "    'def load(path):',\n"
                "    \"    'Read a file.'\",\n"
                "    '    def _inner():',\n"
                "    '        pass',\n"
                "    '    return _inner',\n"
                "    '',\n"
                "    '@register',\n"
                "    'def save(obj):',\n"
                "    '    return obj',\n"
                "    '',\n"
                "    'async def fetch(url):',\n"
                "    \"    'Get a URL.'\",\n"
                "    '    ...',\n"
                "    '',\n"
                "    'class Store:',\n"
                "    \"    'Holds things.'\",\n"
                "    '    def put(self, k, v):',\n"
                "    '        pass',\n"
                "]\n"
                "_mod.write_text('\\n'.join(_lines) + '\\n')"
            ),
            "code": "python_symbols(str(_mod))",
            "note": (
                "the file defines load (line 6), a decorated save, an async fetch and "
                "class Store; save reports its 'def' line 13, not its @register line 12, "
                "and the nested _inner and the method put are absent because neither is "
                "top level"
            ),
        },
        {
            "setup": (
                "_empty = _dir / 'consts.py'\n"
                "_empty.write_text('A = 1\\nB = 2\\n')"
            ),
            "code": "python_symbols(str(_empty))",
            "note": "a module with no def or class gives an empty list, not an error",
        },
        {
            "setup": (
                "_broken = _dir / 'broken.py'\n"
                "_broken.write_text('def oops(:\\n    pass\\n')"
            ),
            "code": "python_symbols(str(_broken))",
            "note": "unparsable source raises rather than returning a partial list",
            "raises": True,
        },
    ],
)
def python_symbols(path: str) -> list[dict]:
    """List the functions and classes a Python file defines at module level.

    Use when: you want to know what a Python file offers — building an index,
        checking that every public function is documented, finding where a name
        is defined — and it must be safe on code you have not vetted. The file is
        parsed, never imported, so nothing in it executes.
    Don't use when: you need methods, nested functions, or anything conditionally
        defined (only module-level `def`/`class` statements are reported — walk
        the tree with `ast` yourself for the rest), you need imported or
        re-exported names (they are not defined here, so they never appear), or
        you need the live objects with their signatures and defaults (import the
        module and use `inspect`).

    Args:
        path: Filesystem path to a .py file. Must exist and be UTF-8 Python
            source for the running interpreter's grammar. Directories, notebooks
            and .pyc files are not accepted.

    Returns:
        list of {name, kind, line, has_docstring} in source order, one per
        top-level definition. `name` is the bare identifier ("load", not
        "mod.load"). `kind` is exactly one of "function", "async function" or
        "class". `line` is the 1-based line of the `def`/`class` keyword itself,
        so a decorated definition points past its decorators. `has_docstring` is
        True when the body's first statement is a string literal. A file with no
        top-level definitions gives an empty list.

    Raises:
        FileNotFoundError: no file at that path.
        IsADirectoryError: path is a directory — pass a single file.
        SyntaxError: the source does not parse. The exception carries filename,
            lineno and offset, so report those rather than guessing; a file using
            newer syntax than this interpreter fails the same way.
        UnicodeDecodeError: the file is not UTF-8. Source with a non-UTF-8
            encoding declaration is out of scope for this helper.

    Preconditions:
        The file is never imported and no code in it runs, so it is safe to point
        at untrusted source.
    """
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)

    symbols: list[dict] = []
    for node in tree.body:
        kind = _KINDS.get(type(node))
        if kind is None:
            continue
        symbols.append(
            {
                "name": node.name,
                "kind": kind,
                "line": node.lineno,
                "has_docstring": ast.get_docstring(node) is not None,
            }
        )
    return symbols
