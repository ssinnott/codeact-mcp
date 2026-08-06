"""Drives the server over real stdio, exactly as an MCP client would."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "server" / "run_server.py"


class Client:
    """A minimal MCP client, so the handshake is tested end to end."""

    def __init__(self, home: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(ENTRY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": "/usr/bin:/bin", "CODEACT_HOME": str(home), "HOME": str(home)},
        )
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
            )
            + "\n"
        )
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, tool_name, /, **args):
        # Positional-only, so a tool taking its own `name` argument still works.
        reply = self.call("tools/call", {"name": tool_name, "arguments": args})
        return reply["result"]["content"][0]["text"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class TestProtocol(unittest.TestCase):
    def setUp(self):
        home = Path(__file__).resolve().parent / ".tmp-home"
        home.mkdir(exist_ok=True)
        self.c = Client(home)
        self.addCleanup(self.c.close)
        reply = self.c.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.init = reply["result"]
        self.c.notify("notifications/initialized")

    def test_initialize_handshake(self):
        self.assertEqual(self.init["serverInfo"]["name"], "codeact")
        self.assertIn("tools", self.init["capabilities"])
        self.assertEqual(self.init["protocolVersion"], "2025-06-18")

    def test_tools_are_listed_with_schemas(self):
        tools = self.c.call("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(
            names,
            {
                "run_python",
                "session_state",
                "restart_session",
                "search_helpers",
                "describe_helper",
                "propose_helper",
                "helper_source",
                "request_capability",
            },
        )
        for tool in tools:
            self.assertTrue(tool["description"].strip())
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_capability_grant_always_prompts_the_human(self):
        # requiresUserInteraction means it can never be pre-allowlisted away.
        tools = {t["name"]: t for t in self.c.call("tools/list")["result"]["tools"]}
        meta = tools["request_capability"].get("_meta") or {}
        self.assertTrue(meta.get("anthropic/requiresUserInteraction"))

    def _bad_call(self, tool_name, **args):
        result = self.c.call("tools/call", {"name": tool_name, "arguments": args})["result"]
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def test_a_misspelled_argument_names_the_real_ones(self):
        # The bare TypeError this used to raise named neither the tool's
        # arguments nor the typo, so the caller had nothing to correct against.
        out = self._bad_call("describe_helper", nmae="read_jsonl")
        self.assertIn("unknown argument 'nmae'", out)
        self.assertIn("accepted: name", out)

    def test_a_missing_required_argument_is_named(self):
        out = self._bad_call("propose_helper", name="shout")
        self.assertIn("missing required argument 'source'", out)

    def test_every_argument_problem_is_reported_at_once(self):
        out = self._bad_call("helper_source", nmae="read_jsonl")
        self.assertIn("missing required argument 'name'", out)
        self.assertIn("missing required argument 'reason'", out)
        self.assertIn("unknown argument 'nmae'", out)

    def test_a_wrong_type_is_refused_rather_than_silently_misread(self):
        # jobs=<string> is a valid keyword argument, so this used to run: the
        # string was iterated character by character and the search quietly
        # matched nothing, which reads as "no such helper" rather than a defect.
        out = self._bad_call("search_helpers", task="read jsonl", jobs="parse")
        self.assertIn("'jobs' must be a array", out)
        self.assertIn("read_jsonl", self.c.tool("search_helpers", task="jsonl", jobs=["parse"]))

    def test_a_value_outside_the_taxonomy_says_what_is_available(self):
        out = self._bad_call("search_helpers", task="anything", jobs=["ingest"])
        self.assertIn("'ingest'", out)
        self.assertIn("parse", out)  # the real vocabulary

    def test_a_bad_enum_on_a_scalar_is_caught_too(self):
        out = self._bad_call("request_capability", capability="everything", reason="why not")
        self.assertIn("must be one of", out)
        self.assertIn("network", out)

    def test_valid_arguments_still_reach_the_handler(self):
        self.assertIn("read_jsonl", self.c.tool("describe_helper", name="read_jsonl"))
        self.assertIn("42", self.c.tool("run_python", code="6 * 7", timeout_s=5))

    def test_propose_helper_reports_every_gate_problem_at_once(self):
        out = self.c.tool(
            "propose_helper",
            name="bad",
            source="from codeact import helper\n\ndef bad(x):\n    return x\n",
        )
        self.assertIn("did NOT pass the gate", out)

    def test_a_complete_proposal_is_accepted_but_not_installed(self):
        source = (
            "from codeact import helper\n\n"
            "@helper(job='transform', domains=['text'], examples=[{'code': \"shout('a')\"}])\n"
            "def shout(text: str) -> str:\n"
            '    """Uppercase a string for emphasis in output.\n\n'
            "    Use when: rendering a heading.\n"
            "    Don't use when: it is already uppercase.\n"
            "    Args:\n        text: any string\n"
            "    Returns:\n        the same string in upper case\n"
            '    """\n'
            "    return text.upper()\n"
        )
        out = self.c.tool("propose_helper", name="shout", source=source)
        self.assertIn("pending review", out)
        self.assertIn("not callable until then", out)
        # Still not in the interpreter.
        self.assertIn("NameError", self.c.tool("run_python", code="shout('x')"))

    def test_helper_source_is_available_for_revision(self):
        out = self.c.tool("helper_source", name="read_jsonl", reason="proposing a revision")
        self.assertIn("def read_jsonl", out)

    def test_search_helpers_returns_index_lines(self):
        out = self.c.tool("search_helpers", task="read a jsonl file of records")
        self.assertIn("read_jsonl", out)
        self.assertIn("describe_helper", out)  # tells the agent the next step

    def test_search_respects_job_filter(self):
        out = self.c.tool("search_helpers", task="anything", jobs=["acquire"])
        self.assertNotIn("read_jsonl", out)  # read_jsonl is `parse`, not `acquire`

    def test_describe_helper_returns_the_contract_not_the_source(self):
        out = self.c.tool("describe_helper", name="read_jsonl")
        self.assertIn("Use when:", out)
        self.assertIn("Don't use when:", out)
        self.assertIn("Returns:", out)
        # The implementation must not leak through the card.
        self.assertNotIn("def read_jsonl", out)
        self.assertNotIn("json.loads", out)

    def test_describe_unknown_helper_suggests_alternatives(self):
        out = self.c.tool("describe_helper", name="jsonl")
        self.assertIn("No helper named", out)
        self.assertIn("read_jsonl", out)  # substring hint

    def test_describe_names_a_helper_that_failed_to_load(self):
        # Silence is the §12 failure: a helper that exists on disk but will not
        # import was indistinguishable from one that never existed, and only
        # the CLI could say which. The two call for different reactions.
        target = Path(__file__).resolve().parent / ".tmp-home" / "helpers" / "wrecked.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "from codeact import helper\n"
            "import nonexistent_module_xyz\n\n"
            "@helper(job='transform', domains=['text'], examples=[{'code': 'wrecked(1)'}])\n"
            "def wrecked(x: int) -> int:\n"
            '    """Double a number."""\n'
            "    return x * 2\n"
        )
        self.addCleanup(target.unlink)
        out = self.c.tool("describe_helper", name="wrecked")
        self.assertIn("failed to load", out)
        self.assertIn("nonexistent_module_xyz", out)

    def test_helpers_are_preloaded_into_the_interpreter(self):
        out = self.c.tool("run_python", code="callable(read_jsonl)")
        self.assertIn("True", out)

    def test_in_interpreter_discovery_facade(self):
        out = self.c.tool("run_python", code="helpers.search('jsonl')")
        self.assertIn("read_jsonl", out)

    def test_a_long_expression_result_is_not_cut_to_a_summary(self):
        # The compact 120-char repr is right for the state delta and wrong for a
        # value the agent explicitly evaluated.
        out = self.c.tool("run_python", code="'-'.join(str(i) for i in range(300))")
        self.assertIn("299", out)

    def test_preloaded_helpers_are_not_reported_as_session_state(self):
        # They are always there; listing them as fresh state would be noise.
        out = self.c.tool("run_python", code="x = 1")
        self.assertNotIn("read_jsonl", out)

    def test_notifications_get_no_reply(self):
        # If a notification were answered, this tools/list reply would be the
        # stale notification response and the ids would drift.
        self.c.notify("notifications/cancelled")
        self.assertIn("tools", self.c.call("tools/list")["result"])

    def test_run_python_round_trip(self):
        self.assertIn("hello", self.c.tool("run_python", code="print('hello')"))

    def test_state_persists_across_tool_calls(self):
        self.c.tool("run_python", code="total = 6 * 7")
        self.assertIn("42", self.c.tool("run_python", code="total"))

    def test_namespace_delta_surfaced_to_the_model(self):
        out = self.c.tool("run_python", code="nums = [1, 2, 3]")
        self.assertIn("state:", out)
        self.assertIn("+nums (list)", out)

    def test_guard_audits_without_blocking(self):
        out = self.c.tool("run_python", code="import subprocess\nprint('ran anyway')")
        self.assertIn("ran anyway", out)  # audit mode must not block
        self.assertIn("audit only", out)
        self.assertIn("subprocess", out)

    def test_tier0_code_produces_no_guard_note(self):
        out = self.c.tool("run_python", code="import json\nprint(json.dumps({'a': 1}))")
        self.assertNotIn("guard", out)

    def test_error_is_reported_not_raised(self):
        out = self.c.tool("run_python", code="1/0")
        self.assertIn("ZeroDivisionError", out)

    def test_restart_clears_state(self):
        self.c.tool("run_python", code="doomed = 1")
        self.c.tool("restart_session")
        self.assertIn("NameError", self.c.tool("run_python", code="doomed"))

    def test_session_state_tool(self):
        self.c.tool("run_python", code="kept = {'k': 'v'}")
        self.assertIn("kept (dict)", self.c.tool("session_state"))

    def test_unknown_method_returns_jsonrpc_error(self):
        reply = self.c.call("nonexistent/method")
        self.assertEqual(reply["error"]["code"], -32601)

    def test_unknown_tool_is_a_tool_error_not_a_crash(self):
        reply = self.c.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("tools", self.c.call("tools/list")["result"])

    def test_corpus_records_executions(self):
        self.c.tool("run_python", code="corpus_probe = 1")
        home = Path(__file__).resolve().parent / ".tmp-home"
        entries = [
            json.loads(line)
            for line in (home / "corpus.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertTrue(any("corpus_probe" in e["code"] for e in entries))
        self.assertTrue(all("project" in e and "outcome" in e for e in entries))

    def test_corpus_redacts_credentials(self):
        self.c.tool("run_python", code="token = 'ghp_' + 'A' * 36")
        home = Path(__file__).resolve().parent / ".tmp-home"
        text = (home / "corpus.jsonl").read_text()
        self.assertNotIn("ghp_AAAA", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
