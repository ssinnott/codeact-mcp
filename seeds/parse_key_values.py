"""Reading .env / KEY=VALUE configuration text."""

from __future__ import annotations

import re

from codeact import helper

_EXPORT_RE = re.compile(r"^export\s+", re.IGNORECASE)
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
_BOM = "\ufeff"  # UTF-8 byte-order mark, invisible in an editor


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
            "code": (
                "parse_key_values('# database\\n"
                "export DB_HOST=localhost\\n"
                "DB_PORT = 5432\\n"
                "\\n"
                "[section]\\n"
                "EMPTY=\\n')"
            ),
            "note": (
                "the comment, blank and `[section]` lines are skipped; `export ` is "
                "dropped; the spaces around `DB_PORT` are stripped off the key, so it "
                "is looked up as 'DB_PORT'; a bare `EMPTY=` gives ''"
            ),
        },
        {
            "code": "parse_key_values('URL=https://h.test/p?a=1&b=2\\nTOKEN=aGVsbG8=\\n')",
            "note": (
                "the line is split on the FIRST `=` only, so every later `=` stays in "
                "the value: query strings and base64 padding survive intact"
            ),
        },
        {
            "code": (
                "parse_key_values('''DOUBLE=\"a\\\\tb\"\\n"
                "SINGLE='a\\\\tb'\\n"
                "HASH=pa#ss\\n"
                "BARE=a #b''')"
            ),
            "note": (
                "a double-quoted value has \\t and friends unescaped (a real tab "
                "here), a single-quoted one is literal, and in a bare value a `#` "
                "after whitespace starts a comment while `pa#ss` keeps its `#`"
            ),
        },
        {
            "code": "parse_key_values('PORT=1\\nport=2\\nAPI_Key=abc', lower_keys=True)",
            "note": (
                "lower_keys folds casing, so PORT and port collide into one entry and "
                "the later line silently wins — no warning that two keys were merged"
            ),
        },
        {
            "code": "parse_key_values('\\ufeffDB_HOST=localhost')",
            "note": (
                "a leading UTF-8 BOM (usual in .env files written on Windows) is "
                "removed, so the first key is 'DB_HOST' and not '\\ufeffDB_HOST'"
            ),
        },
        {
            "code": "parse_key_values(b'DB_HOST=localhost')",
            "raises": True,
            "note": "anything that is not a str is refused up front, not half-parsed",
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
            work. A line is split on the *first* `=` only: everything before it
            is the key, everything after it — later `=` signs included — is the
            value, so `URL=https://h/p?a=1&b=2` and base64 padding like
            `TOKEN=aGVsbG8=` come through whole. Whitespace around the key is
            stripped, so `DB_HOST = localhost` is stored under `DB_HOST` with no
            trailing space. A leading UTF-8 BOM (common in .env files written on
            Windows) is removed before parsing, so it cannot end up glued to the
            first key. Skipped silently: blank lines, lines whose first non-space
            character is `#`, lines with no `=` at all (so `[section]` headers
            vanish), and lines whose key is empty.
        lower_keys: Lowercase every key before storing it. Use when the source
            casing is inconsistent and you want predictable lookups. Default
            False keeps keys exactly as written. Beware: folding can merge keys
            that differed only in case — `PORT=1` then `port=2` leaves a single
            `port` entry holding "2" (last line wins), and nothing warns you that
            two distinct keys became one.

    Returns:
        dict of key -> value, in first-appearance order. Every value is a str and
        is never coerced — "5432" stays a string, "true" stays a string. A bare
        `KEY=` line yields "". A key repeated later in the text keeps the last
        value seen while holding its original position. Input with no settings
        gives an empty dict.

    Raises:
        TypeError: text is not a str — bytes, None, a Path, or a list of lines.
            Nothing is parsed and no partial dict is returned; decode or read the
            source yourself first (`raw.decode('utf-8')`, `path.read_text()`).

    Preconditions:
        text is str, not bytes — decode it yourself first. A non-str argument is
        rejected with TypeError rather than coerced or silently half-read.

    Notes:
        Value handling: surrounding whitespace is stripped. A value wrapped in
        single quotes is taken literally; one wrapped in double quotes has
        \\n, \\t, \\r, \\\\, \\" unescaped. In either case anything after the
        closing quote (typically a comment) is discarded. In an *unquoted* value
        a `#` at the start or after whitespace begins a comment and is dropped,
        so `PASS=a#b` keeps `a#b` but `PASS=a #b` keeps `a`. Only `#` starts a
        comment; `;` does not. An unbalanced opening quote is kept verbatim.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"text must be str, not {type(text).__name__}; decode or read the "
            "config yourself first (raw.decode('utf-8'), path.read_text())"
        )

    if text.startswith(_BOM):
        text = text[len(_BOM) :]

    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")  # first '=' wins
        if not sep:
            continue
        key = _EXPORT_RE.sub("", key).strip()
        if not key:
            continue
        result[key.lower() if lower_keys else key] = _unquote(value.strip())
    return result
