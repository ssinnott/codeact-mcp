"""Interpreter and guard tests. Stdlib unittest only — no test dependencies."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import guard  # noqa: E402
from codeact_mcp.interpreter import Session, Timeout  # noqa: E402


class TestSession(unittest.TestCase):
    def setUp(self):
        self.s = Session()
        self.addCleanup(self.s.stop)

    def run_code(self, code, timeout=15.0):
        return self.s.execute(code, timeout)

    def test_state_persists_across_calls(self):
        self.run_code("x = 41")
        out = self.run_code("x + 1")
        self.assertEqual(out["result"], "42")

    def test_stdout_captured(self):
        out = self.run_code("print('hello')")
        self.assertEqual(out["stdout"].strip(), "hello")

    def test_namespace_delta_reports_new_and_changed(self):
        out = self.run_code("a = 1")
        self.assertEqual([d["name"] for d in out["delta"]], ["a"])
        self.assertEqual(out["delta"][0]["kind"], "new")

        out = self.run_code("a = 2")
        self.assertEqual(out["delta"][0]["kind"], "changed")

    def test_traceback_reported_without_killing_session(self):
        out = self.run_code("1 / 0")
        self.assertIn("ZeroDivisionError", out["error"])
        self.assertIsNone(self.run_code("y = 5")["error"])

    def test_traceback_excludes_worker_frames(self):
        out = self.run_code("def boom():\n    raise ValueError('x')\nboom()")
        self.assertNotIn("worker.py", out["error"])
        self.assertIn("ValueError", out["error"])

    def test_syntax_error_is_not_a_crash(self):
        out = self.run_code("def (:")
        self.assertIn("SyntaxError", out["error"])
        self.assertEqual(self.run_code("1 + 1")["result"], "2")

    def test_definitions_survive(self):
        self.run_code("def double(n):\n    return n * 2")
        self.assertEqual(self.run_code("double(21)")["result"], "42")

    def test_system_exit_does_not_kill_the_worker(self):
        out = self.run_code("import sys; sys.exit(3)")
        self.assertIn("SystemExit", out["error"])
        self.assertEqual(self.run_code("'alive'")["result"], "'alive'")

    def test_large_output_truncated_but_recoverable(self):
        out = self.run_code("print('x' * 50000)")
        self.assertTrue(out["truncated"])
        self.assertLess(len(out["stdout"]), 50000)
        self.assertEqual(self.run_code("len(_out)")["result"], "50001")

    def test_stdout_from_user_code_cannot_corrupt_the_protocol(self):
        # A subprocess writing to fd 1 must not be parsed as a protocol message.
        self.run_code(
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'print(\\'{\\\"id\\\": 999}\\')'])"
        )
        self.assertEqual(self.run_code("2 + 2")["result"], "4")

    def test_timeout_interrupts_but_keeps_state(self):
        self.run_code("keep = 'me'")
        out = self.run_code("import time\nwhile True:\n    time.sleep(0.05)", timeout=1.0)
        self.assertIn("KeyboardInterrupt", out["error"])
        self.assertEqual(self.run_code("keep")["result"], "'me'")

    def test_uninterruptible_code_restarts_the_session(self):
        self.run_code("gone = 1")
        with self.assertRaises(Timeout):
            self.run_code(
                "import signal, time\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "time.sleep(30)",
                timeout=1.0,
            )
        self.assertIn("NameError", self.run_code("gone")["error"])

    def test_state_listing(self):
        self.run_code("alpha = [1, 2, 3]")
        names = {n["name"]: n for n in self.s.state()["names"]}
        self.assertEqual(names["alpha"]["type"], "list")


class TestGuard(unittest.TestCase):
    def test_tier0_imports_are_silent(self):
        self.assertEqual(guard.scan("import json, re\nfrom collections import Counter"), [])

    def test_privileged_imports_flagged(self):
        names = {f.name for f in guard.scan("import subprocess\nimport socket")}
        self.assertEqual(names, {"subprocess", "socket"})

    def test_unknown_module_treated_as_privileged(self):
        (finding,) = guard.scan("import some_random_package")
        self.assertEqual(finding.tier, 2)

    def test_read_only_modules_are_tier1(self):
        (finding,) = guard.scan("import pathlib")
        self.assertEqual(finding.tier, 1)

    def test_escape_attributes_are_tier3(self):
        findings = guard.scan("().__class__.__bases__")
        self.assertEqual({f.name for f in findings}, {"__class__", "__bases__"})
        self.assertTrue(all(f.tier == 3 for f in findings))

    def test_dynamic_execution_flagged(self):
        names = {f.name for f in guard.scan("eval('1')\nexec('x=1')")}
        self.assertEqual(names, {"eval", "exec"})

    def test_all_violations_reported_not_just_the_first(self):
        findings = guard.scan("import socket\nimport subprocess\neval('1')")
        self.assertEqual(len(findings), 3)

    def test_syntax_errors_do_not_raise(self):
        self.assertEqual(guard.scan("def (:"), [])

    def test_submodule_resolves_to_its_root(self):
        (finding,) = guard.scan("import os.path")
        self.assertEqual(finding.tier, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
