"""Card parsing, validation and rendering.

Uses helpers defined inline rather than the real seed library, so these test the
format rather than whatever happens to be in seeds/.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact import helper  # noqa: E402
from codeact_mcp import cards, taxonomy  # noqa: E402


@helper(
    job="transform",
    domains=["data"],
    examples=["tidy([3, 1, 2])"],
)
def tidy(values: list, *, reverse: bool = False) -> list:
    """Sort a list of comparable values into a stable order.

    Use when: you want a sorted copy and the input should stay untouched.
    Don't use when: the list is huge and you can sort in place — use list.sort.

    Args:
        values: Any list whose elements compare with each other. Mixed
            uncomparable types will raise.
        reverse: Sort descending instead of ascending.

    Returns:
        a new list holding the same elements in sorted order.

    Raises:
        TypeError: the elements are not mutually comparable.

    Preconditions:
        Elements must support <.
    """
    return sorted(values, reverse=reverse)


class TestDocstringParsing(unittest.TestCase):
    def setUp(self):
        self.prose = cards.parse_docstring(tidy.__doc__)

    def test_summary_is_the_first_line(self):
        self.assertEqual(
            self.prose.summary, "Sort a list of comparable values into a stable order."
        )

    def test_both_use_when_halves(self):
        self.assertTrue(self.prose.use_when.startswith("you want a sorted copy"))
        self.assertIn("list.sort", self.prose.dont_use_when)

    def test_each_arg_is_a_separate_entry(self):
        self.assertEqual(list(self.prose.args), ["values", "reverse"])

    def test_wrapped_arg_descriptions_are_joined(self):
        # The 'values' description continues on a more-indented line.
        self.assertIn("uncomparable types will raise", self.prose.args["values"])
        self.assertNotIn("reverse", self.prose.args["values"])

    def test_raises_entries_split(self):
        self.assertEqual(list(self.prose.raises), ["TypeError"])

    def test_sections_after_args_still_parse(self):
        self.assertTrue(self.prose.returns.startswith("a new list"))
        self.assertIn("support <", self.prose.preconditions)

    def test_unindented_first_line_does_not_break_dedent(self):
        # The classic docstring trap: line 1 has no indent, the rest do, so
        # textwrap.dedent finds a common prefix of "" and strips nothing.
        doc = "Summary here.\n\n    Use when: always\n    Args:\n        x: a thing\n"
        parsed = cards.parse_docstring(doc)
        self.assertEqual(parsed.summary, "Summary here.")
        self.assertEqual(list(parsed.args), ["x"])

    def test_empty_docstring(self):
        self.assertEqual(cards.parse_docstring(None).summary, "")
        self.assertEqual(cards.parse_docstring("").args, {})


class TestRendering(unittest.TestCase):
    def test_signature_has_real_type_names(self):
        card = cards.build(tidy)
        self.assertIn("values: list", card.signature)
        self.assertNotIn("'list'", card.signature)

    def test_index_line_is_one_line(self):
        line = cards.build(tidy).index_line()
        self.assertNotIn("\n", line)
        self.assertIn("[transform/data]", line)

    def test_render_contains_the_whole_contract(self):
        text = cards.build(tidy).render()
        for expected in ("Use when:", "Don't use when:", "Parameters:", "Returns:", "Raises:"):
            self.assertIn(expected, text)

    def test_render_marks_quarantine_prominently(self):
        card = cards.build(tidy)
        card.quarantine = "examples no longer match"
        text = card.render()
        self.assertIn("QUARANTINED", text)
        self.assertLess(text.index("QUARANTINED"), text.index("Use when:"))

    def test_captured_output_is_labelled_as_real(self):
        card = cards.build(tidy, [{"code": "tidy([2,1])", "output": "[1, 2]", "note": ""}])
        self.assertIn("captured from real runs", card.render())
        self.assertIn("[1, 2]", card.render())

    def test_unverified_examples_are_labelled_differently(self):
        self.assertIn("not yet verified", cards.build(tidy).render())


class TestValidation(unittest.TestCase):
    def test_a_complete_helper_passes(self):
        self.assertEqual(cards.validate(tidy), [])

    def test_undecorated_function_rejected(self):
        def bare(x: int) -> int:
            """Doc."""
            return x

        self.assertIn("not decorated", cards.validate(bare)[0])

    def _problems(self, fn):
        return " | ".join(cards.validate(fn))

    def test_missing_dont_use_when_rejected(self):
        @helper(job="transform", domains=["data"], examples=["f(1)"])
        def f(x: int) -> int:
            """Do a thing to a number.

            Use when: you need it.
            Args:
                x: the number
            Returns:
                the number, changed somehow
            """
            return x

        self.assertIn("Don't use when", self._problems(f))

    def test_undocumented_parameter_rejected(self):
        @helper(job="transform", domains=["data"], examples=["g(1, 2)"])
        def g(x: int, y: int) -> int:
            """Add a pair of numbers together.

            Use when: adding.
            Don't use when: subtracting.
            Args:
                x: the first
            Returns:
                the sum of both inputs
            """
            return x + y

        self.assertIn("'y' is not documented", self._problems(g))

    def test_missing_annotations_rejected(self):
        @helper(job="transform", domains=["data"], examples=["h(1)"])
        def h(x):
            """Pass a value straight back out again.

            Use when: never.
            Don't use when: always.
            Args:
                x: a thing
            Returns:
                the same thing you gave it
            """
            return x

        problems = self._problems(h)
        self.assertIn("no type annotation", problems)
        self.assertIn("no return type annotation", problems)

    def test_return_type_masquerading_as_shape_rejected(self):
        @helper(job="transform", domains=["data"], examples=["k(1)"])
        def k(x: int) -> list:
            """Wrap a value in a list for downstream code.

            Use when: wrapping.
            Don't use when: unwrapping.
            Args:
                x: a thing
            Returns:
                list[dict]
            """
            return [x]

        self.assertIn("describes a type, not a shape", self._problems(k))

    def test_summary_restating_the_signature_rejected(self):
        @helper(job="transform", domains=["data"], examples=["m(1)"])
        def m(x: int) -> int:
            """A function that takes x.

            Use when: whenever.
            Don't use when: never.
            Args:
                x: a thing
            Returns:
                a number of some kind
            """
            return x

        self.assertIn("restates the signature", self._problems(m))

    def test_examples_are_required(self):
        @helper(job="transform", domains=["data"])
        def n(x: int) -> int:
            """Return the value it was given back to the caller.

            Use when: whenever.
            Don't use when: never.
            Args:
                x: a thing
            Returns:
                a number of some kind
            """
            return x

        self.assertIn("no examples", self._problems(n))

    def test_unknown_job_and_domain_rejected(self):
        @helper(job="wrangle", domains=["astrology"], examples=["p(1)"])
        def p(x: int) -> int:
            """Return the value it was given back to the caller.

            Use when: whenever.
            Don't use when: never.
            Args:
                x: a thing
            Returns:
                a number of some kind
            """
            return x

        problems = self._problems(p)
        self.assertIn("unknown job", problems)
        self.assertIn("unknown domain", problems)

    def test_job_inconsistent_with_side_effects_rejected(self):
        @helper(job="transform", domains=["data"], side_effects="network", examples=["q(1)"])
        def q(x: int) -> int:
            """Return the value it was given back to the caller.

            Use when: whenever.
            Don't use when: never.
            Args:
                x: a thing
            Returns:
                a number of some kind
            """
            return x

        self.assertIn("inconsistent with side_effects", self._problems(q))

    def test_too_many_domains_rejected(self):
        @helper(job="transform", domains=["data", "text", "fs", "time"], examples=["r(1)"])
        def r(x: int) -> int:
            """Return the value it was given back to the caller.

            Use when: whenever.
            Don't use when: never.
            Args:
                x: a thing
            Returns:
                a number of some kind
            """
            return x

        self.assertIn("at most 3", self._problems(r))

    def test_every_problem_reported_at_once(self):
        @helper(job="transform", domains=["data"])
        def s(x, y):
            """Thing."""
            return x

        # One round trip should surface all of it, not the first one.
        self.assertGreater(len(cards.validate(s)), 4)


class TestTaxonomy(unittest.TestCase):
    def test_world_touching_jobs_cannot_be_pure(self):
        for job in taxonomy.WORLD_TOUCHING:
            self.assertFalse(taxonomy.job_allows(job, "none"), job)

    def test_pure_jobs_cannot_reach_the_network(self):
        for job in ("transform", "present"):
            self.assertFalse(taxonomy.job_allows(job, "network"), job)

    def test_parsing_a_file_is_still_parsing(self):
        self.assertTrue(taxonomy.job_allows("parse", "filesystem"))

    def test_every_job_has_a_definition(self):
        for job in taxonomy.JOBS:
            self.assertTrue(taxonomy.JOBS[job].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
