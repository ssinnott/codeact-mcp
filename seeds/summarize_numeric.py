"""Descriptive statistics for a column of numbers."""

from __future__ import annotations

import math
import numbers
import statistics
from collections.abc import Sequence

from codeact import helper


def _percentile(ordered: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (numpy default)."""
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo]) * (1.0 - frac) + float(ordered[hi]) * frac


@helper(
    job="transform",
    domains=["data"],
    examples=[
        {
            "code": "summarize_numeric([2, 4, 4, 4, 5, 5, 7, 9])",
            "note": "p90 sits between the 7th and 8th value, so it interpolates to 7.6",
        },
        {
            "code": "summarize_numeric([42.5])",
            "note": "one value: stdev is None because a sample of one has no spread",
        },
        {
            "code": "summarize_numeric([])",
            "note": "empty input is an error, not a zeroed summary — the caller decides",
            "raises": True,
        },
        {
            "code": "summarize_numeric([1.0, float('nan'), 3.0])",
            "note": "a NaN is rejected up front rather than quietly poisoning mean and stdev",
            "raises": True,
        },
    ],
)
def summarize_numeric(values: Sequence[float]) -> dict[str, float | None]:
    """Reduce a column of numbers to the seven figures you would put in a report.

    Use when: you have latencies, sizes, scores or counts and want the shape of
        the distribution in one call — typically to compare groups, or to sanity
        check data before charting it.
    Don't use when: you need a percentile other than the 90th, a histogram, or
        correlation between columns (use the `statistics` module directly), or
        the values are categorical — counting distinct labels is a different job.

    Args:
        values: The numbers to summarise. Any sequence of int or float — a list,
            a tuple, a column pulled out of records. Order is irrelevant; the
            function sorts a copy and never mutates the input. Must be non-empty
            and free of NaN and infinity — see Raises.

    Returns:
        dict with exactly seven keys: `count` (int, how many values were given);
        `min` and `max` (smallest and largest); `mean` (arithmetic mean);
        `median` (middle value, or the average of the middle two when count is
        even); `stdev` (SAMPLE standard deviation, n-1 denominator — and None,
        not 0.0, when count is 1, where it is undefined); and `p90` (90th
        percentile, linearly interpolated between the two neighbouring order
        statistics the way numpy does, so it is usually not one of the input
        values). Everything but `count` and a None `stdev` is a float, even when
        every input was an int.

    Raises:
        ValueError: `values` is empty — there is no meaningful summary of
            nothing, so test for emptiness first and decide what your report
            shows — or it contains a NaN or an infinity, which one bad row in a
            CSV or JSON column will happily supply. The message carries the
            offending index; drop them with
            `[v for v in values if math.isfinite(v)]` and call again.
        TypeError: an element is not a real number — a None or a never-converted
            string, typically. The message carries the element's index, so coerce
            or drop that row and call again.

    Preconditions:
        `values` must be non-empty and every element a finite real number. Both
        are checked before any statistic is computed, so a bad column fails fast
        with a pointed message rather than returning a plausible-looking NaN.
    """
    ordered = list(values)
    for index, value in enumerate(ordered):
        if not isinstance(value, numbers.Real):
            raise TypeError(f"values[{index}] is {type(value).__name__}, not a real number")
        if not math.isfinite(value):
            raise ValueError(f"values[{index}] is {value}; a summary of non-finite values is meaningless")
    if not ordered:
        raise ValueError("summarize_numeric() needs at least one value, got an empty sequence")

    ordered.sort()
    count = len(ordered)
    return {
        "count": count,
        "min": float(ordered[0]),
        "max": float(ordered[-1]),
        "mean": statistics.fmean(ordered),
        "median": float(statistics.median(ordered)),
        "stdev": statistics.stdev(ordered) if count > 1 else None,
        "p90": _percentile(ordered, 0.90),
    }
