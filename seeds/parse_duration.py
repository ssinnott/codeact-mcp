"""Turning compact duration shorthand into seconds."""

from __future__ import annotations

import re

from codeact import helper

_UNITS = {"w": 604800.0, "d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([wdhms])", re.IGNORECASE)


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
            "note": "fractional amounts are fine",
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
            "2h30m". Whitespace is allowed between components and between a
            number and its unit. Components are simply added, so order does not
            matter and repeats accumulate ("1h1h" is two hours). Every number
            must carry a unit, and negatives are not accepted.

    Returns:
        total number of seconds as a float — "2h30m" gives 9000.0. Always >= 0.0,
        and 0.0 only for an explicit zero such as "0s".

    Raises:
        ValueError: text is empty or whitespace, contains a number with no unit
            ("90"), uses an unsupported or long-form unit ("2 hours", "500ms",
            "3y"), or is negative ("-5m"). The message quotes the offending
            remainder of the string, so surface it to whoever wrote the value and
            ask for a corrected one rather than silently substituting a default.

    Preconditions:
        text is str, not an int — pass "90s", not 90.
    """
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
    return total
