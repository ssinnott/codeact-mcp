"""Turning compact duration shorthand into seconds."""

from __future__ import annotations

import math
import re

from codeact import helper

_UNITS = {"w": 604800.0, "d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}

# Both classes are spelled out in ASCII on purpose.
#
# `[wdhms]` under re.IGNORECASE also matches U+017F ('ſ', LATIN SMALL LETTER
# LONG S) because Unicode case folding maps it to 's' — but str.lower() leaves
# it alone, so the unit lookup raised KeyError instead of the documented
# ValueError. `\d` has the same shape of problem: it matches every Unicode
# decimal digit, so '1۵s' quietly parsed as 15 seconds. Listing the accepted
# characters keeps the regex and the dict lookup agreeing on exactly one
# alphabet, and pushes everything else down the ValueError path.
_TOKEN_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([WwDdHhMmSs])")


@helper(
    job="parse",
    domains=["time"],
    side_effects="none",
    examples=[
        {
            "code": "parse_duration('2h30m')",
            "note": "components sum: 2*3600 + 30*60",
        },
        {
            "code": "parse_duration('1d 12h')",
            "note": "spaces are allowed; a day is exactly 86400 seconds",
        },
        {
            "code": "parse_duration('1.5h')",
            "note": "fractional amounts are fine, written with a leading digit",
        },
        {
            "code": "parse_duration('90')",
            "note": "a bare number is rejected — the unit is never assumed, write '90s'",
            "raises": True,
        },
        {
            "code": "parse_duration('500ms')",
            "note": "milliseconds are not a supported unit; 'm' consumes 500 minutes, then 's' has no number",
            "raises": True,
        },
        {
            "code": "parse_duration('.5h')",
            "note": "the number is plain decimal digits only — write '0.5h'. '+5m', "
            "'1_000s' and '1e3s' are rejected the same way, so a value that came "
            "from a Python literal may need rewriting",
            "raises": True,
        },
        {
            "code": "parse_duration('5\\u017f')",
            "note": "U+017F ('long s') case-folds to 's' in Unicode but is not the unit "
            "'s'; only ASCII wdhms and ASCII 0-9 count, so lookalikes and "
            "non-ASCII digits are rejected rather than silently accepted",
            "raises": True,
        },
        {
            "code": "parse_duration(90)",
            "note": "a non-str argument is a TypeError, not a parse failure — there is no "
            "unit to read off an int",
            "raises": True,
        },
        {
            "code": "parse_duration('1' + '0' * 400 + 's')",
            "note": "a total too large for a float is rejected, never returned as inf — "
            "the result is always safe to hand to time.sleep",
            "raises": True,
        },
    ],
)
def parse_duration(text: str) -> float:
    """Turn compact duration shorthand like '2h30m' into a count of seconds.

    Use when: a config value, CLI flag, env var, or CI setting expresses a
        timeout, interval, or retention window in human shorthand and you need a
        number to hand to time.sleep, a `timeout=` argument, or arithmetic.
    Don't use when: the input is an ISO-8601 duration ("PT2H30M"), a timestamp,
        or a date range — this only understands `<number><unit>` runs. Also not
        for calendar arithmetic: a day here is exactly 86400 s and a week exactly
        7 days, so for "one month later" or anything that must survive a DST
        change, do the arithmetic with datetime objects instead.

    Args:
        text: One or more `<number><unit>` components, e.g. "90s", "2h30m",
            "1d12h", "1.5h". Units are w (weeks), d (days), h (hours),
            m (minutes), s (seconds), case-insensitive — "2H30M" is the same as
            "2h30m" — and only those five ASCII letters; no long forms
            ("2 hours") and no Unicode lookalikes. Each number is plain ASCII
            digits, optionally followed by "." and more digits: "0.5h", not
            ".5h". Signs ("+5m", "-5m"), exponents ("1e3s"), underscores
            ("1_000s"), thousands separators and non-ASCII digits are all
            rejected, so a value copied from a Python literal or a localized
            config may need rewriting first. Whitespace is allowed between
            components and between a number and its unit. Components are simply
            added, so order does not matter and repeats accumulate ("1h1h" is
            two hours). Every number must carry a unit and every unit must carry
            a number, so "90" and "w" are both rejected.

    Returns:
        total number of seconds as a float — "2h30m" gives 9000.0. Always finite
        and >= 0.0 — never inf or nan, so it is safe to pass straight to
        time.sleep — and 0.0 only for explicit zeros such as "0s".

    Raises:
        TypeError: text is not a str. Ints and floats are rejected rather than
            read as seconds, because the unit is never assumed: pass "90s".
        ValueError: text is empty or whitespace, contains a number with no unit
            ("90"), uses an unsupported or long-form unit ("2 hours", "500ms",
            "3y", "5ſ"), is signed ("-5m", "+5m"), writes the number in a
            form only Python accepts (".5h", "1e3s", "1_000s"), or adds up to
            more seconds than a float can hold. For a syntax error the message
            quotes the offending remainder of the string; surface it to whoever
            wrote the value and ask for a corrected one rather than silently
            substituting a default.

    Preconditions:
        none beyond the argument types — any str is safe to pass, and anything
        unparseable raises rather than returning a guess.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"parse_duration expects a str like '90s', got {type(text).__name__}: "
            f"{text!r} — the unit is never assumed"
        )

    raw = text.strip()
    if not raw:
        raise ValueError("cannot parse duration: the value is empty")

    total = 0.0
    pos = 0
    seen = False
    while pos < len(raw):
        if raw[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(raw, pos)
        if not match:
            raise ValueError(
                f"cannot parse duration {text!r}: expected <number><unit> at "
                f"{raw[pos:]!r} (units: w, d, h, m, s)"
            )
        total += float(match.group(1)) * _UNITS[match.group(2).lower()]
        pos = match.end()
        seen = True

    if not seen:
        raise ValueError(f"cannot parse duration {text!r}: no <number><unit> components found")
    if not math.isfinite(total):
        # Returning inf here would satisfy ">= 0.0" and then hang whatever slept
        # on it. Any component over ~1e308 seconds gets us here.
        raise ValueError(
            "cannot parse duration: the components add up to more seconds than a "
            "float can hold (over 1e308) — check for a runaway number of digits"
        )
    return total
