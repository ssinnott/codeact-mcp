"""Counting lines, words and characters in a block of text."""

from __future__ import annotations

from codeact import helper


@helper(
    job="inspect",
    domains=["text"],
    side_effects="none",
    examples=[
        {
            "code": "text_stats('hello world\\n\\n  the quick brown fox jumped\\n')",
            "note": (
                "3 lines: the trailing newline does not start a fourth, and the "
                "blank middle line counts in lines but not non_blank_lines"
            ),
        },
        {
            "code": "text_stats('')",
            "note": "empty text is all zeros; longest_line_number is 0 because there is no line 1",
        },
        {
            "code": "text_stats('a\\tb  c')",
            "note": (
                "words splits on any run of whitespace, so the tab and the double "
                "space each separate one word; characters counts the tab as 1"
            ),
        },
    ],
)
def text_stats(text: str) -> dict:
    """Measure a block of text: how many lines, words and characters it holds.

    Use when: you need the size or shape of some text before deciding what to do
        with it — is this file big enough to chunk, did the generated output
        blow past a line-length limit, how much did an edit add or remove.
    Don't use when: you want word or token frequencies (count them yourself with
        collections.Counter), LLM tokens (characters are not tokens — use a
        tokenizer), or per-line detail such as which lines are over budget (this
        reports only the single longest).

    Args:
        text: The text to measure, as one str. Pass file contents you have
            already read, not a path. Line endings may be \\n or \\r\\n; both are
            treated as line breaks and neither is counted in a line's length.

    Returns:
        dict with six int keys: `lines` (total lines, where a trailing newline
        does not create an extra empty one), `non_blank_lines` (lines with at
        least one non-whitespace character), `words` (whitespace-separated runs,
        so punctuation stays attached to its word), `characters` (len of the
        input, including every space and newline), `longest_line_length` (length
        of the longest line, newline excluded), and `longest_line_number` (its
        1-based line number). For "" every value is 0, and
        longest_line_number is 0 whenever there are no lines — real line numbers
        start at 1, so 0 is unambiguous.

    Notes:
        On a tie the earliest longest line wins. Lengths are counts of Python
        characters (code points): a tab is 1 regardless of display width, and an
        emoji or accented letter is 1 even though it is several bytes on disk.
    """
    lines = text.splitlines()

    longest_length = len(lines[0]) if lines else 0
    longest_number = 1 if lines else 0
    for index, line in enumerate(lines, start=1):
        if len(line) > longest_length:
            longest_length = len(line)
            longest_number = index

    return {
        "lines": len(lines),
        "non_blank_lines": sum(1 for line in lines if line.strip()),
        "words": len(text.split()),
        "characters": len(text),
        "longest_line_length": longest_length,
        "longest_line_number": longest_number,
    }
