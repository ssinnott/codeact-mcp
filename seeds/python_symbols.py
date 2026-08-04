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
                "    '',\n"
                "    \"if os.name == 'nt':\",\n"
                "    '    def win_only():',\n"
                "    '        pass',\n"
                "    '',\n"
                "    'Alias = Store',\n"
                "]\n"
                "_mod.write_text('\\n'.join(_lines) + '\\n')"
            ),
            "code": "python_symbols(str(_mod))",
            "note": (
                "the four direct children of the module body are load, the decorated "
                "save (reported at its 'def' line 13, not its @register line 12), the "
                "async fetch and class Store; _inner (nested), put (a method), win_only "
                "(inside an if) and Alias (an assignment, not a def) are all absent"
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
                "_bom = _dir / 'bom.py'\n"
                "_bom.write_text(\"def start():\\n    'Go.'\\n\", encoding='utf-8-sig')"
            ),
            "code": "python_symbols(str(_bom))",
            "note": (
                "a file saved with a UTF-8 BOM — as Notepad and several Windows editors "
                "do — is read fine: the BOM is stripped before parsing, so start is "
                "still found at line 1 and no SyntaxError is raised"
            ),
        },
        {
            "setup": (
                "_broken = _dir / 'broken.py'\n"
                "_broken.write_text('def oops(:\\n    pass\\n')"
            ),
            "code": "python_symbols(str(_broken))",
            "note": "genuinely unparsable source raises rather than returning a partial list",
            "raises": True,
        },
        {
            "setup": "_missing = _dir / 'not_here.py'",
            "code": "python_symbols(str(_missing))",
            "note": "a path with no file at it raises rather than returning an empty list",
            "raises": True,
        },
        {
            "code": "python_symbols(str(_dir))",
            "note": "a directory is not a source file, so a wrong path is loud on POSIX",
            "raises": True,
        },
        {
            "setup": (
                "_latin = _dir / 'latin1.py'\n"
                "_latin.write_bytes(b'x = \"caf\\xe9\"\\n')"
            ),
            "code": "python_symbols(str(_latin))",
            "note": (
                "bytes that are not UTF-8 (here latin-1) raise while decoding, before "
                "any parsing; a '# -*- coding: latin-1 -*-' line would not change that"
            ),
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
    Don't use when: you need anything that is not a direct child of the module
        body. Methods, nested functions, and any `def`/`class` written inside an
        `if`, `try`, `with`, `for` or `while` block are all skipped, even though
        such a definition really is created when the module runs and even when it
        sits at the top of the file — walk the tree with `ast` yourself for those.
        Skip it too when you want names bound by assignment (`f = lambda: ...`,
        `Alias = Store`) or by `import`, which are not `def`/`class` statements
        and so never appear, or when you need live objects with their signatures
        and defaults (import the module and use `inspect`).

    Args:
        path: Filesystem path to a .py file, absolute or relative to the current
            working directory. Must exist, be a file rather than a directory, and
            hold UTF-8 Python source that parses under the running interpreter's
            grammar. A leading UTF-8 BOM is accepted and ignored, exactly as
            CPython accepts it. The extension is not checked — content is what
            matters, so a .txt of Python source works and a notebook or .pyc
            simply fails to parse.

    Returns:
        list of {name, kind, line, has_docstring} in source order, one per
        top-level definition. `name` is the bare identifier ("load", not
        "mod.load"). `kind` is exactly one of "function", "async function" or
        "class", chosen from the statement alone: decorators are never evaluated,
        so a `@property`-decorated `def` is still reported as "function". `line`
        is the 1-based line of the `def`/`class` keyword itself, so a decorated
        definition points past its decorators. `has_docstring` is True when the
        body's first statement is a string literal. Decorated top-level
        definitions are included; a file with no top-level definitions gives an
        empty list.

    Raises:
        FileNotFoundError: nothing exists at that path.
        IsADirectoryError: the path names a directory rather than a file. On
            Windows this same mistake surfaces as PermissionError.
        SyntaxError: the source genuinely does not parse — including source
            written for a newer Python than this interpreter, and source
            containing NUL bytes. The exception carries filename, lineno and
            offset, so report those rather than guessing. A UTF-8 BOM is not a
            syntax error here.
        UnicodeDecodeError: the bytes are not UTF-8 (latin-1 or UTF-16, say).
            Raised while decoding, before any parsing. A `# -*- coding: ... -*-`
            line declaring another encoding is not honoured; such files are out
            of scope for this helper.

    Preconditions:
        The file is never imported and no code in it runs, so it is safe to point
        at untrusted source.

    Notes:
        Those four are the only exceptions that escape. Anything that decodes and
        parses returns a list — never None, never a partial result, and never a
        placeholder entry for a definition it could not read.
    """
    # utf-8-sig, not utf-8: a BOM'd file is valid Python to CPython, but a
    # leading U+FEFF left in the string makes ast.parse raise SyntaxError. The
    # codec strips the BOM when present and is plain UTF-8 otherwise, so
    # genuinely non-UTF-8 bytes still raise UnicodeDecodeError.
    source = Path(path).read_text(encoding="utf-8-sig")
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
