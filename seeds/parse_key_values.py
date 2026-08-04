"""Reading .env / KEY=VALUE configuration text."""

from __future__ import annotations

import re

from codeact import helper

_EXPORT_RE = re.compile(r"^export\s+", re.IGNORECASE)
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


def _unquote(value: str) -> str:
    """Strip surrounding quotes, or an inline comment if the value is bare."""
    if not value:
        return ""
    quote = value[0]
    if quote not in "\"'":
        cut = _INLINE_COMMENT_RE.search(value)
        return (value[: cut.start()] if cut else value).strip()

    out: list[str] = []
    i = 1
    while i < len(value):
        char = value[i]
        if char == "\\" and quote == '"' and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i + 1], "\\" + value[i + 1]))
            i += 2
            continue
        if char == quote:
            return "".join(out)
        out.append(char)
        i += 1
    return value.strip()  # opened but never closed: keep it literally


@helper(
    job="parse",
    domains=["text"],
    side_effects="none",
    examples=[
        {
            "setup": (
                "_env = '''# database\n"
                "export DB_HOST=localhost\n"
                "DB_PORT = 5432\n"
                "\n"
                "GREETING=\"hello\\\\nworld\"   # trailing comment\n"
                "LITERAL='keep $this as-is'\n"
                "[section]\n"
                "EMPTY=\n"
                "'''"
            ),
            "code": "parse_key_values(_env)",
            "note": (
                "comments, blanks and the `[section]` line are skipped; "
                "`export ` is dropped and quotes are removed"
            ),
        },
        {
            "code": "parse_key_values('API_Key=abc\\nAPI_Key=xyz', lower_keys=True)",
            "note": "keys folded to lowercase; a repeated key keeps the last value",
        },
        {
            "code": "parse_key_values('A=pa#ss\\nB=pa #ss\\nC=\"pa #ss\"')",
            "note": (
                "in a bare value a `#` after whitespace starts a comment and is cut; "
                "quote the value to keep it"
            ),
        },
    ],
)
def parse_key_values(text: str, *, lower_keys: bool = False) -> dict[str, str]:
    """Read .env / ini-style KEY=VALUE text into a flat dict of strings.

    Use when: you already hold the *contents* of a .env file, a block of
        `export FOO=bar` shell lines, or a simple ini-ish config as a string, and
        you want the settings as a dict to read defaults from.
    Don't use when: the file has real ini sections whose keys collide across
        sections (use configparser — section headers are ignored here and the
        result is flat), the format is YAML/TOML/JSON (use those parsers), or you
        need `$VAR` interpolation or the values pushed into os.environ — this
        never expands variables and never touches the environment.

    Args:
        text: The whole config as one already-decoded string, e.g.
            `pathlib.Path('.env').read_text()`. Each setting is `KEY=VALUE` on
            its own line, optionally prefixed with `export `. \\r\\n and \\n both
            work. Skipped silently: blank lines, lines whose first non-space
            character is `#`, lines with no `=` at all (so `[section]` headers
            vanish), and lines whose key is empty.
        lower_keys: Lowercase every key before storing it. Use when the source
            casing is inconsistent and you want predictable lookups. Default
            False keeps keys exactly as written.

    Returns:
        dict of key -> value, in first-appearance order. Every value is a str and
        is never coerced — "5432" stays a string, "true" stays a string. A bare
        `KEY=` line yields "". A key repeated later in the text keeps the last
        value seen. Input with no settings gives an empty dict.

    Preconditions:
        text is str, not bytes — decode it yourself first.

    Notes:
        Value handling: surrounding whitespace is stripped. A value wrapped in
        single quotes is taken literally; one wrapped in double quotes has
        \\n, \\t, \\r, \\\\, \\" unescaped. In either case anything after the
        closing quote (typically a comment) is discarded. In an *unquoted* value
        a `#` at the start or after whitespace begins a comment and is dropped,
        so `PASS=a#b` keeps `a#b` but `PASS=a #b` keeps `a`. Only `#` starts a
        comment; `;` does not. An unbalanced opening quote is kept verbatim.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = _EXPORT_RE.sub("", key).strip()
        if not key:
            continue
        result[key.lower() if lower_keys else key] = _unquote(value.strip())
    return result
