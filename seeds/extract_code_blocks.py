"""Pulling fenced code blocks out of markdown."""

from __future__ import annotations

import re

from codeact import helper

_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _dedent(line: str, width: int) -> str:
    """Drop up to `width` leading spaces, matching the fence's indentation."""
    cut = 0
    while cut < width and cut < len(line) and line[cut] == " ":
        cut += 1
    return line[cut:]


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
            "note": "every fence, in document order; the untagged block gets lang ''",
        },
        {
            "code": "extract_code_blocks(_md, lang='JSON')",
            "note": "the lang filter is case-insensitive and matches ~~~ fences too",
        },
        {
            "code": "extract_code_blocks(_md, lang='')",
            "note": "lang='' selects exactly the fences that carried no language tag",
        },
        {
            "code": "extract_code_blocks('no fences here, just prose')",
            "note": "no blocks is an empty list, not an error",
        },
    ],
)
def extract_code_blocks(markdown: str, *, lang: str | None = None) -> list[dict]:
    """Pull the fenced code blocks out of a markdown document with their languages.

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
        lang: Keep only blocks whose language tag equals this, compared
            case-insensitively after stripping ("python", "json", "PY" all work
            as written). None (default) returns every block including untagged
            ones. Untagged blocks never match a non-None lang — pass "" to
            select exactly the untagged ones.

    Returns:
        list of {lang, code, start_line} in document order. `lang` is the first
        whitespace-separated word of the fence's info string, lowercased, or ""
        when the fence had none — the rest of the info string is dropped, so
        ```python title=x reports just "python". `code` is the block body as one
        string with no
        trailing newline ("" for an empty block). `start_line` is the 1-based
        line number of the *opening fence* in `markdown`, so the body begins at
        start_line + 1. No matching blocks gives an empty list.

    Preconditions:
        markdown is str, not bytes or a file path — read the file yourself first.

    Notes:
        A closing fence must use the same character as its opener and be at least
        as long, so a ``` block nested inside a ~~~ block survives as content. A
        fence that is never closed runs to the end of the document rather than
        being dropped. Body lines are de-indented by the opening fence's own
        indentation, so an indented block comes back flush left.
    """
    wanted = lang.strip().lower() if lang is not None else None
    lines = markdown.splitlines()
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
        if wanted is None or block_lang == wanted:
            blocks.append(
                {"lang": block_lang, "code": "\n".join(body), "start_line": start_line}
            )
    return blocks
