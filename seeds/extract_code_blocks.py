"""Pulling fenced code blocks out of markdown."""

from __future__ import annotations

import re
from collections.abc import Collection

from codeact import helper

_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _dedent(line: str, width: int) -> str:
    """Drop up to `width` leading spaces, matching the fence's indentation."""
    cut = 0
    while cut < width and cut < len(line) and line[cut] == " ":
        cut += 1
    return line[cut:]


def _split_lines(text: str) -> list[str]:
    r"""Split on \n, \r\n and lone \r — and on nothing else.

    str.splitlines() would also break on form feed, \x85, \u2028 and friends,
    which turns a form feed inside a code block into a newline in `code` and
    shifts every start_line after it. Those characters are ordinary content
    here, so line endings are normalised explicitly instead.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline ends the last line, it does not add one
    return lines


def _wanted_tags(lang: str | Collection[str] | None) -> set[str] | None:
    """Normalise the `lang` filter into a set of tags, or None for "keep all"."""
    if lang is None:
        return None
    if isinstance(lang, str):
        return {lang.strip().lower()}
    if isinstance(lang, (bytes, bytearray)) or not isinstance(lang, Collection):
        raise TypeError(
            "lang must be None, a str, or a collection of str (list/tuple/set); "
            f"got {type(lang).__name__}"
        )
    tags = list(lang)
    for tag in tags:
        if not isinstance(tag, str):
            raise TypeError(
                f"every lang tag must be a str; got {type(tag).__name__} ({tag!r})"
            )
    if not tags:
        raise ValueError(
            "lang is an empty collection, which would match nothing; pass "
            'lang=None to keep every block, or lang="" to keep the untagged ones'
        )
    return {tag.strip().lower() for tag in tags}


@helper(
    job="parse",
    domains=["text"],
    side_effects="none",
    examples=[
        {
            "setup": (
                "_md = '''# Title\n"
                "\n"
                "```python\n"
                "print(\"hi\")\n"
                "```\n"
                "\n"
                "Prose between blocks.\n"
                "\n"
                "```\n"
                "echo untagged\n"
                "```\n"
                "\n"
                "~~~json\n"
                "{\"a\": 1}\n"
                "~~~\n"
                "'''"
            ),
            "code": "extract_code_blocks(_md)",
            "note": "_md is a 14-line README holding a ```python fence, an untagged ``` fence and a ~~~json fence; every fence comes back in document order and the untagged one gets lang ''",
        },
        {
            "code": "extract_code_blocks(_md, lang='JSON')",
            "note": "one tag, matched exactly after stripping and lowercasing both sides — 'JSON' finds _md's ~~~json fence",
        },
        {
            "code": "extract_code_blocks(_md, lang='')",
            "note": "lang='' selects exactly the fences that carried no language tag",
        },
        {
            "setup": (
                "_readme = '''```sh\n"
                "pip install thing\n"
                "```\n"
                "\n"
                "```py\n"
                "import thing\n"
                "```\n"
                "\n"
                "```python3\n"
                "import thing\n"
                "```\n"
                "'''"
            ),
            "code": "extract_code_blocks(_readme, lang='python')",
            "note": "_readme tags its three blocks ```sh, ```py and ```python3 — no alias table, so 'python' matches none of them and the result is empty rather than an error",
        },
        {
            "code": "extract_code_blocks(_readme, lang=('py', 'python', 'python3'))",
            "note": "same _readme: pass every spelling you accept and a block is kept if its tag equals any of them (the ```sh block still isn't one)",
        },
        {
            "code": "extract_code_blocks('```py\\r\\n\\r\\nx = 1\\r\\n\\r\\n```\\r\\n')",
            "note": "CRLF is normalised, so no \\r survives; blank lines inside the fences are kept, so code both starts and ends with \\n",
        },
        {
            "code": "extract_code_blocks('no fences here, just prose')",
            "note": "no blocks is an empty list, not an error",
        },
        {
            "code": "extract_code_blocks(b'```py\\nx\\n```')",
            "raises": True,
            "note": "bytes (or a Path, or a file object) is refused loudly instead of silently finding nothing",
        },
        {
            "code": "extract_code_blocks(_md, lang=[])",
            "raises": True,
            "note": "an empty tag collection is a caller bug, not a request for zero blocks",
        },
        {
            "code": "extract_code_blocks(_md, lang=['py', 3])",
            "raises": True,
            "note": "every tag must be a str",
        },
    ],
)
def extract_code_blocks(
    markdown: str, *, lang: str | Collection[str] | None = None
) -> list[dict]:
    r"""Pull the fenced code blocks out of a markdown document with their languages.

    Use when: you have markdown — a README, an issue body, a model's reply — and
        you want the code inside it: to run it, write it to a file, lint it, or
        count how many examples of a given language a document contains.
    Don't use when: you need the prose, headings, or block order relative to
        surrounding text (parse the document with a real markdown parser), or the
        code is in 4-space indented blocks or inline `backtick spans` — only
        ``` / ~~~ fences are recognised and everything else is invisible here.

    Args:
        markdown: The full document as one string. A fence is a line whose first
            non-space content is three or more backticks or three or more tildes,
            indented by at most 3 spaces; anything else is ignored as prose.
            Anything that is not a str (bytes, Path, file object, None) raises
            TypeError rather than being coerced. Line endings are normalised
            first: \r\n and lone \r become \n, so a CRLF document yields the same
            result as the LF one and no \r is ever left on a line of `code`.
            Nothing else is treated as a line break — a form feed, \x85 or
            \u2028 inside a block stays in `code` as itself.
        lang: Which language tags to keep, or None (default) to keep every block
            including untagged ones. Either one str, or a collection of str
            (list/tuple/set) to accept several spellings at once — a block is
            kept when its tag equals ANY of them. Matching is exact after both
            sides are normalised: each tag you pass is stripped of surrounding
            whitespace and lowercased, and the block's tag is its info string's
            first whitespace-separated word, lowercased. There is no alias
            table and no prefix matching, so lang="python" ignores ```py and
            ```python3 — pass ("py", "python", "python3") if you want those.
            Untagged blocks match only the empty tag: pass "" (or a
            whitespace-only str, which strips to "") to select exactly them. An
            empty collection raises ValueError instead of matching nothing.

    Returns:
        list of {lang, code, start_line} in document order. `lang` is the first
        whitespace-separated word of the fence's info string, lowercased, or ""
        when the fence had none — the rest of the info string is dropped, so
        ```python title=x reports just "python". `code` is the block's body
        lines joined with "\n", verbatim: nothing is stripped, so a blank line
        just inside the opening or closing fence survives as a leading or
        trailing "\n", and `code` ends with a newline only when its last body
        line is blank ("" for a block with no body lines at all). `start_line`
        is the 1-based line number of the *opening fence* in `markdown`, so the
        body begins at start_line + 1. No matching blocks gives an empty list.

    Raises:
        TypeError: `markdown` is not a str; or `lang` is neither None, a str,
            nor a collection of str; or a tag inside that collection is not a
            str. Decode bytes and read files yourself before calling.
        ValueError: `lang` is an empty collection — that would match nothing,
            which is nearly always a caller bug. Pass None to keep every block.

    Preconditions:
        markdown is str, not bytes or a file path — read the file yourself first.

    Notes:
        A closing fence must use the same character as its opener and be at least
        as long, so a ``` block nested inside a ~~~ block survives as content. A
        fence that is never closed runs to the end of the document rather than
        being dropped. Body lines are de-indented by the opening fence's own
        indentation, so an indented block comes back flush left.
    """
    if not isinstance(markdown, str):
        raise TypeError(
            f"markdown must be a str, got {type(markdown).__name__}; decode bytes "
            "or read the file yourself first"
        )
    wanted = _wanted_tags(lang)

    lines = _split_lines(markdown)
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        opening = _FENCE_RE.match(lines[i])
        if not opening:
            i += 1
            continue

        info = opening.group("info").strip()
        char = opening.group("fence")[0]
        width = len(opening.group("fence"))
        indent = len(opening.group("indent"))
        start_line = i + 1

        body: list[str] = []
        i += 1
        while i < len(lines):
            closing = _FENCE_RE.match(lines[i])
            if (
                closing
                and closing.group("fence")[0] == char
                and len(closing.group("fence")) >= width
                and not closing.group("info").strip()
            ):
                i += 1
                break
            body.append(_dedent(lines[i], indent))
            i += 1

        block_lang = info.split()[0].lower() if info else ""
        if wanted is None or block_lang in wanted:
            blocks.append(
                {"lang": block_lang, "code": "\n".join(body), "start_line": start_line}
            )
    return blocks
