"""Loading, quarantine, shadowing, and contract-test execution."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import contract, registry  # noqa: E402

GOOD = '''
"""A helper for tests."""
from __future__ import annotations
from codeact import helper

@helper(job="transform", domains=["data"], examples=[{"code": "shout('hi')"}])
def shout(text: str) -> str:
    """Uppercase a string for emphasis in output.

    Use when: rendering a heading.
    Don't use when: the text is already uppercase.
    Args:
        text: any string
    Returns:
        the same string in upper case
    """
    return text.upper()
'''


def write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body).lstrip())
    return path


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "helpers").mkdir()
        (home / "seeds").mkdir()
        self.helpers = home / "helpers"
        self.seeds = home / "seeds"

        self._prev = os.environ.get("CODEACT_HOME")
        os.environ["CODEACT_HOME"] = str(home)
        # Point the builtin seed directory at a temp one too, so these tests
        # assert on the loader rather than on whatever the library ships.
        self._prev_seeds = registry.SEEDS_DIR
        registry.SEEDS_DIR = self.seeds

    def tearDown(self):
        registry.SEEDS_DIR = self._prev_seeds
        if self._prev is None:
            os.environ.pop("CODEACT_HOME", None)
        else:
            os.environ["CODEACT_HOME"] = self._prev

    def load(self):
        return registry.Registry().load()


class TestLoading(RegistryCase):
    def test_helper_is_discovered(self):
        write(self.helpers, "shout", GOOD)
        self.assertIsNotNone(self.load().get("shout"))

    def test_undecorated_functions_are_ignored(self):
        write(self.helpers, "mixed", GOOD + "\n\ndef _internal(x):\n    return x\n")
        reg = self.load()
        self.assertIn("shout", reg.entries)
        self.assertNotIn("_internal", reg.entries)

    def test_underscore_files_skipped(self):
        write(self.helpers, "_private", GOOD)
        self.assertEqual(self.load().entries, {})

    def test_broken_file_is_reported_not_raised(self):
        write(self.helpers, "broken", "this is not python(")
        write(self.helpers, "shout", GOOD)
        reg = self.load()
        self.assertTrue(any("broken" in e for e in reg.errors))
        self.assertIn("shout", reg.entries)  # one bad file must not hide the rest

    def test_user_helper_shadows_a_seed_of_the_same_name(self):
        write(self.seeds, "shout", GOOD)
        write(self.helpers, "shout", GOOD.replace("text.upper()", "text.title()"))
        entry = self.load().get("shout")
        self.assertFalse(entry.builtin)
        self.assertEqual(entry.fn("hi there"), "Hi There")

    def test_seeds_are_loaded_as_builtin(self):
        write(self.seeds, "shout", GOOD)
        entry = self.load().get("shout")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.builtin)

    def test_namespace_exposes_callables(self):
        write(self.helpers, "shout", GOOD)
        self.assertEqual(self.load().namespace()["shout"]("hi"), "HI")

    def test_counts_by_job(self):
        write(self.helpers, "shout", GOOD)
        self.assertEqual(self.load().counts_by_job().get("transform"), 1)


class TestQuarantine(RegistryCase):
    def test_source_change_since_capture_quarantines(self):
        path = write(self.helpers, "shout", GOOD)
        registry.write_sidecar(path, [{"code": "shout('hi')", "output": "'HI'", "ok": True}])
        self.assertFalse(self.load().get("shout").quarantined)

        path.write_text(path.read_text().replace("text.upper()", "text.lower()"))
        entry = self.load().get("shout")
        self.assertTrue(entry.quarantined)
        self.assertIn("source changed", entry.card.quarantine)

    def test_quarantined_helper_is_hidden_from_search_and_namespace(self):
        path = write(self.helpers, "shout", GOOD)
        registry.write_sidecar(path, [], quarantine="examples fail")
        reg = self.load()
        self.assertNotIn("shout", [e.name for e in reg.available()])
        self.assertNotIn("shout", reg.namespace())

    def test_quarantined_helper_still_answers_describe(self):
        # Silently vanishing would leave anything referencing it with no
        # explanation; the card says why instead.
        path = write(self.helpers, "shout", GOOD)
        registry.write_sidecar(path, [], quarantine="examples fail")
        entry = self.load().get("shout")
        self.assertIsNotNone(entry)
        self.assertIn("QUARANTINED", entry.card.render())

    def test_unreadable_sidecar_is_ignored_not_fatal(self):
        path = write(self.helpers, "shout", GOOD)
        path.with_suffix(".json").write_text("{not json")
        self.assertIsNotNone(self.load().get("shout"))

    def test_captured_output_reaches_the_card(self):
        path = write(self.helpers, "shout", GOOD)
        registry.write_sidecar(path, [{"code": "shout('hi')", "output": "'HI'", "ok": True}])
        self.assertIn("'HI'", self.load().get("shout").card.render())


class TestContractExecution(RegistryCase):
    def test_examples_run_and_capture_real_output(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": "shout('hi')"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "'HI'")

    def test_setup_runs_before_the_example(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(
            path, [{"setup": "_word = 'abc'", "code": "shout(_word)"}]
        )
        self.assertEqual(result["output"], "'ABC'")

    def test_failing_example_is_marked_not_raised(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": "shout(None)"}])
        self.assertFalse(result["ok"])
        self.assertIn("AttributeError", result["output"])

    def test_expected_failure_counts_as_success(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": "shout(None)", "raises": True}])
        self.assertTrue(result["ok"])

    def test_expected_failure_that_succeeds_is_a_defect(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": "shout('hi')", "raises": True}])
        self.assertFalse(result["ok"])
        self.assertIn("expected an exception", result["output"])

    def test_statement_examples_work_not_just_expressions(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": "print(shout('hi'))"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "HI")

    def test_example_printing_the_protocol_marker_cannot_corrupt_results(self):
        path = write(self.helpers, "shout", GOOD)
        results = contract.run_examples(
            path, [{"code": "print(shout('a') + '\\x00CODEACT\\x00[]')"}]
        )
        # An example's own prints are captured, so this never reaches the real
        # stdout at all — the marker just ends up in the recorded output.
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])

    def test_subprocess_emitting_the_marker_cannot_forge_results(self):
        # A subprocess inherits the real stdout, so this genuinely does reach
        # the parent. Parsing the LAST marker is what keeps it harmless.
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(
            path,
            [
                {
                    "setup": "import subprocess, sys",
                    "code": (
                        "subprocess.run([sys.executable, '-c', "
                        "'print(chr(0) + \"CODEACT\" + chr(0) + \"[{}]\")'])"
                    ),
                }
            ],
        )
        self.assertTrue(result["ok"], result["output"])
        self.assertEqual(result["code"][:11], "subprocess.")

    def test_quotes_and_backslashes_in_example_code_survive(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(path, [{"code": r"""shout('a\'b "c" \\d')"""}])
        self.assertTrue(result["ok"], result["output"])

    def test_timeout_does_not_hang_the_caller(self):
        path = write(self.helpers, "shout", GOOD)
        (result,) = contract.run_examples(
            path, [{"setup": "import time", "code": "time.sleep(30)"}], timeout=2.0
        )
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["output"])

    def test_no_examples_is_not_an_error(self):
        path = write(self.helpers, "shout", GOOD)
        self.assertEqual(contract.run_examples(path, []), [])

    def test_compare_detects_drift(self):
        captured = [{"code": "shout('hi')", "output": "'HI'"}]
        fresh = [{"code": "shout('hi')", "output": "'hi'"}]
        self.assertTrue(contract.compare(captured, fresh))
        self.assertFalse(contract.compare(captured, captured))


class TestSidecar(RegistryCase):
    def test_sidecar_records_the_hash_it_was_captured_from(self):
        path = write(self.helpers, "shout", GOOD)
        registry.write_sidecar(path, [])
        payload = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(payload["source_hash"], registry.source_hash(path.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
