"""Retrieval: filtering, ranking, and the priors."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact import helper  # noqa: E402
from codeact_mcp import cards, search  # noqa: E402
from codeact_mcp.registry import Entry  # noqa: E402


def make(name, job, domains, summary, use_when="doing that thing"):
    def fn():  # pragma: no cover - never called
        return None

    fn.__name__ = name
    fn.__doc__ = (
        f"{summary}\n\n"
        f"Use when: {use_when}\n"
        f"Don't use when: something else applies.\n"
        f"Returns:\n    a value of the expected shape\n"
    )
    helper(job=job, domains=domains, examples=[f"{name}()"])(fn)
    return Entry(card=cards.build(fn), fn=fn, path=Path(f"{name}.py"), builtin=True)


POOL = [
    make("read_jsonl", "parse", ["data", "fs"], "Read a JSON Lines file into records."),
    make("write_jsonl", "mutate", ["data", "fs"], "Write records to a JSON Lines file."),
    make("group_by", "transform", ["data"], "Group records into buckets by a key."),
    make("find_files", "inspect", ["fs"], "Walk a tree returning matching file paths."),
    make("to_markdown_table", "present", ["data", "text"], "Render records as a markdown table."),
    make("parse_duration", "parse", ["time"], "Parse a human duration into seconds."),
]


class TestFiltering(unittest.TestCase):
    def test_job_filter_is_hard(self):
        found = search.search(POOL, jobs=["parse"])
        self.assertEqual({e.name for e in found}, {"read_jsonl", "parse_duration"})

    def test_domain_is_a_boost_not_a_filter(self):
        # Cross-cutting helpers have no honest domain, so a domain filter would
        # exclude the right answer for a reasonable query. It ranks instead.
        found = search.search(POOL, domains=["time"])
        self.assertEqual(found[0].name, "parse_duration")
        self.assertEqual(len(found), len(POOL))

    def test_domain_boost_orders_within_a_job(self):
        found = search.search(POOL, jobs=["parse"], domains=["fs"])
        self.assertEqual(found[0].name, "read_jsonl")
        self.assertIn("parse_duration", [e.name for e in found])

    def test_a_wrong_domain_does_not_lose_the_only_candidate(self):
        # The retrieval eval's one miss: an orchestrate-job helper tagged `time`
        # searched for with domains=["http"] must still be found.
        found = search.search(POOL, jobs=["parse"], domains=["http"])
        self.assertTrue(found)

    def test_impossible_job_returns_empty(self):
        self.assertEqual(search.search(POOL, jobs=["generate"]), [])

    def test_no_filters_returns_everything(self):
        self.assertEqual(len(search.search(POOL)), len(POOL))

    def test_limit_is_respected(self):
        self.assertEqual(len(search.search(POOL, limit=2)), 2)


class TestRanking(unittest.TestCase):
    def rank(self, task, **kw):
        return [e.name for e in search.search(POOL, task=task, **kw)]

    def test_lexical_match_wins(self):
        self.assertEqual(self.rank("read a jsonl file")[0], "read_jsonl")

    def test_ranking_discriminates_read_from_write(self):
        self.assertEqual(self.rank("write records out to a jsonl file")[0], "write_jsonl")

    def test_snake_case_names_match_their_parts(self):
        self.assertEqual(self.rank("markdown table")[0], "to_markdown_table")

    def test_empty_query_is_stable_not_random(self):
        self.assertEqual(self.rank(""), sorted(e.name for e in POOL))

    def test_single_helper_pool_does_not_divide_by_zero(self):
        self.assertEqual(len(search.search(POOL[:1], task="anything at all")), 1)

    def test_empty_pool(self):
        self.assertEqual(search.search([], task="anything"), [])

    def test_stopwords_do_not_dominate(self):
        # "I want to get the ..." should not out-rank on filler alone.
        self.assertEqual(self.rank("I want to get all of the files in a directory")[0], "find_files")


class TestPriors(unittest.TestCase):
    def test_failures_push_a_helper_down(self):
        usage = {"read_jsonl": {"calls": 10, "failures": 9}}
        ranked = [e.name for e in search.search(POOL, task="jsonl", usage=usage)]
        self.assertEqual(ranked[0], "write_jsonl")

    def test_usage_breaks_a_tie(self):
        usage = {"group_by": {"calls": 50}}
        ranked = [e.name for e in search.search(POOL, usage=usage)]
        self.assertEqual(ranked[0], "group_by")

    def test_single_project_helper_ranks_lower_elsewhere(self):
        usage = {"group_by": {"calls": 5, "projects": ["/other/repo"]}}
        here = [e.name for e in search.search(POOL, project="/my/repo", usage=usage)]
        self.assertNotEqual(here[0], "group_by")

    def test_cross_project_helper_is_boosted(self):
        usage = {"group_by": {"calls": 5, "projects": ["/a", "/b", "/c"]}}
        ranked = [e.name for e in search.search(POOL, project="/my/repo", usage=usage)]
        self.assertEqual(ranked[0], "group_by")

    def test_affinity_is_a_ranking_signal_not_a_filter(self):
        # A single-project helper must still be findable from elsewhere.
        usage = {"parse_duration": {"calls": 3, "projects": ["/other"]}}
        found = search.search(POOL, task="parse a duration into seconds",
                              project="/my/repo", usage=usage)
        self.assertIn("parse_duration", [e.name for e in found])


if __name__ == "__main__":
    unittest.main(verbosity=2)
