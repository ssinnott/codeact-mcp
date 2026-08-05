"""The session transcript: what was recorded, and what it must never record.

Two things are being protected here. The record has to be complete enough to
answer "what happened" after the fact — including the parts that didn't run —
and it has to be incapable of taking a live session down or of writing a
credential to disk.
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from codeact_mcp import cli, paths  # noqa: E402


def invoke(*argv):
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue()


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="codeact-trace-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        previous = os.environ.get("CODEACT_HOME")
        os.environ["CODEACT_HOME"] = str(self.home)
        self.addCleanup(
            lambda: os.environ.__setitem__("CODEACT_HOME", previous)
            if previous is not None
            else os.environ.pop("CODEACT_HOME", None)
        )
        # A fresh module per test: the session id and the "have I written the
        # header yet" counter are process-global by design.
        from codeact_mcp import trace as trace_mod

        self.trace = importlib.reload(trace_mod)
        self.trace.begin("test")

    def events(self):
        return self.trace.read(self.trace.path())


class WhatIsRecorded(Case):
    def test_nothing_is_written_until_something_happens(self):
        # An MCP server that starts and is never used should leave no file.
        self.assertFalse(self.trace.path().exists())

    def test_the_header_lands_before_the_first_event(self):
        self.trace.executed("1 + 1", outcome="ok", duration_ms=3, timeout_s=30)
        events = self.events()
        self.assertEqual(events[0]["event"], "session")
        self.assertEqual(events[0]["n"], 1)
        self.assertEqual(events[1]["event"], "run")

    def test_the_header_says_which_rules_were_in_force(self):
        # Without this the transcript cannot distinguish a block that was
        # allowed from one that today's config would refuse.
        self.trace.executed("x = 1", outcome="ok", duration_ms=1, timeout_s=30)
        header = self.events()[0]
        for key in ("guard", "granted", "run_as", "commands", "project", "version"):
            self.assertIn(key, header)

    def test_a_run_carries_what_the_session_saw_come_back(self):
        self.trace.executed(
            "print('hi')\nx = 2",
            outcome="ok",
            duration_ms=7,
            timeout_s=30,
            payload={
                "stdout": "hi\n",
                "result": "2",
                "delta": [{"name": "x", "kind": "new", "type": "int", "repr": "2"}],
            },
        )
        run = self.events()[1]
        self.assertEqual(run["stdout"], "hi\n")
        self.assertEqual(run["result"], "2")
        self.assertEqual(run["delta"][0]["name"], "x")
        self.assertEqual(run["duration_ms"], 7)

    def test_events_are_numbered_in_order_with_no_gaps(self):
        self.trace.executed("a = 1", outcome="ok", duration_ms=1, timeout_s=30)
        self.trace.inspected(4)
        self.trace.restarted("requested")
        self.trace.granted("network", "fetch the schema")
        self.trace.source_read("read_jsonl", "revising it")
        self.trace.end()
        numbers = [e["n"] for e in self.events()]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        kinds = [e["event"] for e in self.events()]
        self.assertEqual(
            kinds, ["session", "run", "state", "restart", "grant", "source", "end"]
        )

    def test_a_blocked_block_is_still_part_of_the_record(self):
        # What the guard refused is the most interesting line in an audit, and
        # it is exactly the line that never reaches the interpreter.
        self.trace.executed(
            "import socket", outcome="blocked", duration_ms=0, timeout_s=30, note="refused: socket"
        )
        run = self.events()[1]
        self.assertEqual(run["outcome"], "blocked")
        self.assertIn("socket", run["note"])

    def test_summary_counts_what_went_wrong(self):
        self.trace.executed("a", outcome="ok", duration_ms=10, timeout_s=30)
        self.trace.executed("b", outcome="error", duration_ms=5, timeout_s=30)
        self.trace.executed("c", outcome="blocked", duration_ms=0, timeout_s=30)
        self.trace.executed("d", outcome="timeout", duration_ms=30000, timeout_s=30)
        self.trace.restarted("timeout")
        s = self.trace.summarize(self.events())
        self.assertEqual((s["runs"], s["errors"], s["blocked"], s["timeouts"]), (4, 1, 1, 1))
        self.assertEqual(s["restarts"], 1)
        self.assertEqual(s["duration_ms"], 30015)
        self.assertFalse(s["complete"])

    def test_a_session_that_closed_cleanly_says_so(self):
        self.trace.executed("a", outcome="ok", duration_ms=1, timeout_s=30)
        self.trace.end()
        self.assertTrue(self.trace.summarize(self.events())["complete"])

    def test_a_partial_last_line_does_not_break_reading(self):
        # A killed session leaves a half-written line; the transcript of a
        # session that died is the one most likely to be read.
        self.trace.executed("a", outcome="ok", duration_ms=1, timeout_s=30)
        with self.trace.path().open("a", encoding="utf-8") as fh:
            fh.write('{"n": 3, "event": "run", "co')
        self.assertEqual(len(self.events()), 2)


class WhatIsNeverRecorded(Case):
    def test_credentials_are_redacted_from_code_and_from_output(self):
        secret = "sk-" + "a" * 32
        self.trace.executed(
            f"token = {secret!r}",
            outcome="error",
            duration_ms=1,
            timeout_s=30,
            payload={"stderr": f"auth failed for {secret}", "error": f"Bad token {secret}"},
        )
        text = self.trace.path().read_text()
        self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)

    def test_a_pathological_output_is_bounded(self):
        self.trace.executed(
            "x", outcome="ok", duration_ms=1, timeout_s=30, payload={"stdout": "z" * 50000}
        )
        self.assertLess(len(self.events()[1]["stdout"]), self.trace.MAX_FIELD + 200)

    def test_tracing_off_writes_nothing(self):
        from codeact_mcp import config

        config.save({**config.DEFAULTS, "trace": False})
        self.trace._enabled = None
        self.trace.executed("secret_work()", outcome="ok", duration_ms=1, timeout_s=30)
        self.assertFalse(self.trace.path().exists())

    def test_a_broken_log_never_reaches_the_caller(self):
        # Logging must not be why a working session fails.
        self.trace.paths = None  # any AttributeError inside _write
        try:
            self.trace.executed("a", outcome="ok", duration_ms=1, timeout_s=30)
        finally:
            self.trace.paths = paths

    def test_traces_are_gitignored(self):
        paths.ensure()
        self.assertIn("traces/", (self.home / ".gitignore").read_text())

    def test_an_older_install_learns_to_ignore_them(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".gitignore").write_text("corpus.jsonl\nfingerprints.db\n*.log\n")
        paths.ensure()
        body = (self.home / ".gitignore").read_text()
        self.assertIn("traces/", body)
        self.assertIn("corpus.jsonl", body)


class TheCommand(Case):
    def populate(self):
        self.trace.executed(
            "import json\nx = 1",
            outcome="ok",
            duration_ms=12,
            timeout_s=30,
            payload={"stdout": "loaded\n", "delta": [{"name": "x", "kind": "new", "type": "int", "repr": "1"}]},
        )
        self.trace.executed(
            "open('/etc/shadow')", outcome="blocked", duration_ms=0, timeout_s=30, note="refused: open"
        )
        self.trace.executed(
            "x.missing",
            outcome="error",
            duration_ms=2,
            timeout_s=30,
            payload={"error": "AttributeError: 'int' object has no attribute 'missing'"},
        )
        self.trace.end()

    def test_listing_with_nothing_recorded(self):
        code, text = invoke("trace")
        self.assertEqual(code, 0)
        self.assertIn("no traces yet", text)

    def test_listing_shows_each_session(self):
        self.populate()
        code, text = invoke("trace")
        self.assertEqual(code, 0)
        self.assertIn(self.trace.SESSION_ID, text)
        self.assertIn("3 run(s)", text)

    def test_last_resolves_to_the_most_recent(self):
        self.populate()
        code, text = invoke("trace", "last")
        self.assertEqual(code, 0)
        self.assertIn("import json", text)
        self.assertIn("AttributeError", text)
        self.assertIn("loaded", text)
        self.assertIn("guard", text)  # the rules line

    def test_a_prefix_resolves_to_the_session(self):
        self.populate()
        code, text = invoke("trace", self.trace.SESSION_ID[:6])
        self.assertEqual(code, 0)
        self.assertIn(self.trace.SESSION_ID, text)

    def test_an_unknown_session_is_reported_not_raised(self):
        self.populate()
        code, text = invoke("trace", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no trace matching", text)

    def test_code_mode_emits_the_python_and_flags_what_did_not_run(self):
        self.populate()
        code, text = invoke("trace", "last", "--code")
        self.assertEqual(code, 0)
        self.assertIn("import json", text)
        # A blocked block stays in, commented, so the script cannot read as a
        # clean run of something that was actually refused.
        self.assertIn("# open('/etc/shadow')", text)
        self.assertNotIn("loaded", text)  # output is not code

    def test_long_output_is_abbreviated_unless_asked(self):
        self.trace.executed(
            "dump()",
            outcome="ok",
            duration_ms=1,
            timeout_s=30,
            payload={"stdout": "\n".join(f"line {i}" for i in range(80))},
        )
        _, short = invoke("trace", "last")
        self.assertIn("more line(s)", short)
        _, full = invoke("trace", "last", "--full")
        self.assertIn("line 79", full)

    def test_prune_removes_old_traces(self):
        self.populate()
        target = self.trace.path()
        os.utime(target, (0, 0))
        code, text = invoke("trace", "--prune", "30")
        self.assertEqual(code, 0)
        self.assertIn("removed 1", text)
        self.assertFalse(target.exists())

    def test_status_mentions_the_trace(self):
        self.populate()
        _, text = invoke("status")
        self.assertIn("traces", text)


class EndToEnd(unittest.TestCase):
    """Through the real MCP tool surface and a real interpreter subprocess."""

    def test_a_session_records_what_it_ran(self):
        with tempfile.TemporaryDirectory(prefix="codeact-trace-e2e-") as tmp:
            previous = os.environ.get("CODEACT_HOME")
            os.environ["CODEACT_HOME"] = tmp
            try:
                from codeact_mcp import trace as trace_mod

                trace = importlib.reload(trace_mod)
                import codeact_mcp.server as server_mod

                server = importlib.reload(server_mod)
                trace.begin(server.VERSION)

                built = server.build()
                call = built._handlers
                call["run_python"]("x = 6 * 7\nprint('done')\nx")
                call["session_state"]()
                call["run_python"]("x.nope")
                server._session.stop()

                events = trace.read(trace.path())
                kinds = [e["event"] for e in events]
                self.assertEqual(kinds, ["session", "run", "state", "run"])

                first = events[1]
                self.assertEqual(first["outcome"], "ok")
                self.assertIn("done", first["stdout"])
                self.assertEqual(first["result"], "42")
                self.assertEqual(first["delta"][0]["name"], "x")

                self.assertEqual(events[3]["outcome"], "error")
                self.assertIn("AttributeError", events[3]["error"])
            finally:
                if previous is not None:
                    os.environ["CODEACT_HOME"] = previous
                else:
                    os.environ.pop("CODEACT_HOME", None)
                importlib.reload(trace_mod)
                importlib.reload(server_mod)


if __name__ == "__main__":
    unittest.main()
