"""The one command a human runs.

These are wiring tests, deliberately. Each subcommand's real work is covered
where it lives — what breaks when nine scripts become one CLI is dispatch: a
command that parses but reaches no handler, a group whose bare form crashes
instead of printing something, an entry point that can't find the package.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from codeact_mcp import cli, paths  # noqa: E402


def invoke(*argv):
    """Run the CLI in-process, returning (exit code, everything printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # argparse's own exits
            code = int(exc.code or 0)
    return code, out.getvalue()


class EntryPoint(unittest.TestCase):
    def test_the_script_runs_as_a_program(self):
        # The shim is the only part these tests cannot reach in-process, and
        # it is exactly the part that breaks when the package layout moves.
        done = subprocess.run(
            [sys.executable, str(ROOT / "codeact"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("codeact", done.stdout)

    def test_the_script_is_executable(self):
        self.assertTrue(os.access(ROOT / "codeact", os.X_OK))

    def test_paths_can_find_it(self):
        self.assertTrue(paths.cli().exists())


class Dispatch(unittest.TestCase):
    def test_every_command_reaches_a_handler(self):
        parser = cli.build_parser()
        [subparsers] = [
            a for a in parser._actions if isinstance(a, type(parser._subparsers._group_actions[0]))
        ]
        self.assertTrue(subparsers.choices, "no subcommands registered")
        for name, sub in subparsers.choices.items():
            with self.subTest(command=name):
                # Either the command handles itself, or it is a group whose
                # bare form still does something rather than raising.
                nested = [a for a in sub._actions if getattr(a, "choices", None)]
                if not nested:
                    self.assertIn("handler", sub._defaults, f"{name} has no handler")
                else:
                    self.assertIn("handler", sub._defaults, f"group {name} has no default")

    def test_help_renders_for_every_command(self):
        parser = cli.build_parser()
        for name in parser._subparsers._group_actions[0].choices:
            with self.subTest(command=name):
                code, text = invoke(name, "--help")
                self.assertEqual(code, 0)
                self.assertIn("usage", text)

    def test_an_unknown_command_is_an_error_not_a_traceback(self):
        code, text = invoke("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", text)


class AgainstAnEmptyLibrary(unittest.TestCase):
    """A fresh machine is the state most likely to produce a stack trace."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="codeact-cli-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        previous = os.environ.get("CODEACT_HOME")
        os.environ["CODEACT_HOME"] = str(self.home)
        self.addCleanup(
            lambda: os.environ.__setitem__("CODEACT_HOME", previous)
            if previous is not None
            else os.environ.pop("CODEACT_HOME", None)
        )

    def test_bare_invocation_reports_the_state(self):
        code, text = invoke()
        self.assertEqual(code, 0)
        for label in ("helpers", "proposals", "guard", "commands", "run_as", "secrets"):
            self.assertIn(label, text)

    def test_status_says_what_is_governed_and_how(self):
        _, text = invoke("status")
        self.assertIn("off", text)  # the command policy ships off
        self.assertIn("kubectl", text)

    def test_editing_config_does_not_leak_into_the_next_read(self):
        # `codeact policy enforce` mutates a nested dict inside the loaded
        # settings. If load() hands out the module defaults, every later reader
        # in the process — including a running MCP server — inherits the edit.
        invoke("policy", "enforce")
        os.environ["CODEACT_HOME"] = str(self.home / "elsewhere")
        _, text = invoke("status")
        self.assertIn("commands   off", text)

    def test_pending_is_empty_not_broken(self):
        code, text = invoke("pending")
        self.assertEqual(code, 0)
        self.assertIn("nothing pending", text)

    def test_approving_something_that_does_not_exist(self):
        code, text = invoke("approve", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no proposal", text)

    def test_secret_list_with_nothing_set(self):
        code, text = invoke("secret")
        self.assertEqual(code, 0)
        self.assertIn("no secrets set", text)

    def test_policy_round_trip_writes_config(self):
        self.assertEqual(invoke("policy", "enforce")[0], 0)
        self.assertEqual(invoke("policy", "block", "terraform")[0], 0)
        settings = json.loads((self.home / "config.json").read_text())
        self.assertEqual(settings["commands"]["mode"], "enforce")
        self.assertIn("terraform", settings["commands"]["rules"])

        _, text = invoke("policy")
        self.assertIn("terraform", text)

        self.assertEqual(invoke("policy", "unblock", "terraform")[0], 0)
        settings = json.loads((self.home / "config.json").read_text())
        self.assertNotIn("terraform", settings["commands"]["rules"])

    def test_policy_check_decides_without_running_anything(self):
        code, text = invoke(
            "policy", "check", "--codeact", "kubectl --context prod delete pod x"
        )
        self.assertEqual(code, 1)  # non-zero: it would be refused
        self.assertIn("BLOCKED", text)

    def test_policy_log_before_anything_was_logged(self):
        code, text = invoke("policy", "log")
        self.assertEqual(code, 0)
        self.assertIn("nothing recorded yet", text)


class AgainstTheSeedLibrary(unittest.TestCase):
    """CODEACT_HOME is empty here too, so these read the shipped seeds."""

    def test_card_index_lists_the_jobs(self):
        code, text = invoke("card", "--index")
        self.assertEqual(code, 0)
        self.assertIn("Jobs:", text)
        self.assertIn("transform", text)

    def test_card_prints_one_helper(self):
        code, text = invoke("card", "read_jsonl")
        self.assertEqual(code, 0)
        self.assertIn("read_jsonl(", text)

    def test_card_for_something_that_is_not_a_helper(self):
        code, text = invoke("card", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no helper named", text)

    def test_mine_with_an_empty_corpus_explains_itself(self):
        code, text = invoke("mine")
        self.assertEqual(code, 0)
        self.assertIn("corpus", text)

    def test_eval_datasets_default_to_the_ones_in_the_repo(self):
        code, text = invoke("eval", "retrieval")
        self.assertEqual(code, 0)
        self.assertIn("hit@1", text)

    def test_a_missing_dataset_is_reported_not_raised(self):
        code, text = invoke("eval", "cards", "/nonexistent/cards.json")
        self.assertEqual(code, 2)
        self.assertIn("no such dataset", text)


if __name__ == "__main__":
    unittest.main()
