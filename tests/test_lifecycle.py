"""Phases 3-5: proposals, the guard's enforcement path, secrets, mining, trials."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import (  # noqa: E402
    config,
    interpreter,
    guard,
    miner,
    paths,
    proposals,
    registry,
    secrets_store,
    trial,
)

GOOD = '''"""Slugify."""
from __future__ import annotations
import re
from codeact import helper

@helper(job="transform", domains=["text"],
        examples=[{"code": "slugify('Hello, World!')"},
                  {"code": "slugify('!!!')", "raises": True}])
def slugify(text: str, *, sep: str = "-") -> str:
    """Turn arbitrary text into a URL-safe slug.

    Use when: building a filename or URL fragment from a human title.
    Don't use when: you need the original back — this is lossy.
    Args:
        text: Any string containing at least one alphanumeric character.
        sep: Character joining the words together.
    Returns:
        a lowercase string of words joined by sep.
    Raises:
        ValueError: nothing alphanumeric to build a slug from.
    """
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", text) if w]
    if not words:
        raise ValueError("nothing alphanumeric to slugify")
    return sep.join(w.lower() for w in words)
'''


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "helpers").mkdir()
        (home / "seeds").mkdir()
        self.seeds = home / "seeds"
        self.home = home

        self._env = {k: os.environ.get(k) for k in ("CODEACT_HOME", "CODEACT_SECRETS")}
        os.environ["CODEACT_HOME"] = str(home)
        os.environ["CODEACT_SECRETS"] = str(home / "secrets.json")
        self._seeds_dir = registry.SEEDS_DIR
        registry.SEEDS_DIR = self.seeds
        registry.registry(reload=True)

    def tearDown(self):
        registry.SEEDS_DIR = self._seeds_dir
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        registry.registry(reload=True)


class TestProposalGate(Case):
    def test_a_complete_proposal_passes_and_installs_nothing(self):
        p = proposals.propose("slugify", GOOD)
        self.assertEqual(p.problems, [])
        self.assertEqual(p.status, proposals.PENDING)
        self.assertIsNone(registry.registry(reload=True).get("slugify"))

    def test_examples_are_executed_and_captured(self):
        p = proposals.propose("slugify", GOOD)
        self.assertEqual([e["ok"] for e in p.examples], [True, True])
        self.assertIn("hello-world", p.card)

    def test_approval_installs_and_makes_it_callable(self):
        p = proposals.propose("slugify", GOOD)
        ok, message = proposals.approve(p.id)
        self.assertTrue(ok, message)
        entry = registry.registry(reload=True).get("slugify")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.fn("Hello, World!"), "hello-world")

    def test_a_failing_proposal_cannot_be_approved(self):
        bad = GOOD.replace("    Don't use when: you need the original back — this is lossy.\n", "")
        p = proposals.propose("slugify", bad)
        self.assertEqual(p.status, proposals.REJECTED)
        ok, message = proposals.approve(p.id)
        self.assertFalse(ok)
        self.assertIn("did not pass the gate", message)

    def test_source_that_does_not_import_is_reported_not_raised(self):
        p = proposals.propose("broken", "this is not python(")
        self.assertTrue(any("does not import" in x for x in p.problems))

    def test_name_must_match_a_decorated_function(self):
        p = proposals.propose("nope", GOOD)
        self.assertTrue(any("defines no @helper" in x for x in p.problems))

    def test_duplicate_name_requires_revises(self):
        proposals.approve(proposals.propose("slugify", GOOD).id)
        p = proposals.propose("slugify", GOOD)
        self.assertTrue(any("already exists" in x for x in p.problems))

    def test_revision_of_a_missing_helper_is_rejected(self):
        p = proposals.propose("slugify", GOOD, revises="ghost")
        self.assertTrue(any("no such helper" in x for x in p.problems))

    def test_failed_example_blocks_approval(self):
        broken = GOOD.replace(
            'raise ValueError("nothing alphanumeric to slugify")', 'return "oops"'
        )
        p = proposals.propose("slugify", broken)
        self.assertTrue(any("example failed" in x for x in p.problems))

    def test_reject_records_a_reason(self):
        p = proposals.propose("slugify", GOOD)
        proposals.reject(p.id, "not general enough")
        self.assertEqual(proposals.load(p.id).reason, "not general enough")

    def test_reject_without_a_reason_still_counts_as_decided(self):
        # The review app rejects with no reason at all, so `dismissed` rather
        # than a non-empty `reason` is what marks a human decision.
        p = proposals.propose("slugify", GOOD)
        proposals.reject(p.id)
        self.assertTrue(proposals.load(p.id).dismissed)

    def test_a_dismissed_proposal_leaves_the_failed_tab(self):
        from codeact_mcp.cli import review

        broken = GOOD.replace(
            'raise ValueError("nothing alphanumeric to slugify")', 'return "oops"'
        )
        p = proposals.propose("slugify", broken)
        self.assertTrue(p.problems)
        self.assertIn(p.id, [x["id"] for x in review.state()["failed"]])
        proposals.reject(p.id)
        self.assertNotIn(p.id, [x["id"] for x in review.state()["failed"]])

    def test_capability_escalation_in_a_revision_is_flagged(self):
        proposals.approve(proposals.propose("slugify", GOOD).id)
        risky = GOOD.replace(
            'domains=["text"],',
            'domains=["text"], side_effects="network", requires_secrets=["TOKEN"],',
        ).replace('job="transform"', 'job="acquire"')
        p = proposals.propose("slugify", risky, revises="slugify")
        self.assertTrue(p.diff.get("escalates"), p.problems)
        self.assertIn("TOKEN", p.diff.get("secrets_gained", []))


class TestReviewableAtEveryExit(Case):
    """Whatever the gate decides, the human has to be able to see the result.

    Both cases here are gate exits that used to return before the status was
    decided, so the proposal was filed as pending and — having produced no card
    — showed up in the reviewer's queue as an entry with nothing in it.
    """

    def test_source_that_does_not_import_is_filed_as_rejected(self):
        p = proposals.propose("broken", "this is not python(")
        self.assertEqual(p.status, proposals.REJECTED)
        self.assertEqual(proposals.load(p.id).status, proposals.REJECTED)

    def test_source_without_the_named_helper_is_filed_as_rejected(self):
        p = proposals.propose("nope", GOOD)
        self.assertEqual(p.status, proposals.REJECTED)

    def test_a_failed_proposal_does_not_sit_in_the_pending_queue(self):
        p = proposals.propose("broken", "this is not python(")
        self.assertNotIn(p.id, [x.id for x in proposals.listing(proposals.PENDING)])
        self.assertIn(p.id, [x.id for x in proposals.listing(proposals.REJECTED)])

    def test_the_review_app_shows_it_under_failed_with_its_problems(self):
        from codeact_mcp.cli import review

        p = proposals.propose("broken", "this is not python(")
        state = review.state()
        self.assertEqual([x["id"] for x in state["pending"]], [])
        shown = [x for x in state["failed"] if x["id"] == p.id]
        self.assertEqual(len(shown), 1)
        # No card is renderable here, so the problems are the whole explanation.
        self.assertTrue(shown[0]["problems"])

    def test_an_unknown_field_does_not_make_a_proposal_disappear(self):
        p = proposals.propose("slugify", GOOD)
        raw = json.loads(p.path.read_text())
        raw["invented_by_a_later_version"] = True
        p.path.write_text(json.dumps(raw))

        loaded = proposals.load(p.id)
        self.assertIsNotNone(loaded, "a proposal must survive a field it does not know")
        self.assertEqual(loaded.name, "slugify")
        self.assertIn(p.id, [x.id for x in proposals.listing()])

    def test_a_missing_field_falls_back_rather_than_vanishing(self):
        p = proposals.propose("slugify", GOOD)
        raw = json.loads(p.path.read_text())
        del raw["created"]
        p.path.write_text(json.dumps(raw))
        self.assertEqual(proposals.load(p.id).name, "slugify")

    def test_a_record_with_no_id_is_the_one_thing_refused(self):
        # Without an id there is no approve or reject to offer.
        paths.ensure()
        path = paths.proposals_dir() / "headless.json"
        path.write_text(json.dumps({"name": "ghost", "status": "pending"}))
        self.assertEqual(proposals.listing(), [])

    def test_a_broken_api_request_answers_instead_of_dropping_the_socket(self):
        from codeact_mcp.cli import review

        original = review.state
        review.state = lambda: (_ for _ in ()).throw(RuntimeError("disk on fire"))
        self.addCleanup(setattr, review, "state", original)

        server = ThreadingHTTPServer(("127.0.0.1", 0), review.Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        url = f"http://127.0.0.1:{server.server_port}/api/state?t={review.TOKEN}"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=10)
        self.assertEqual(caught.exception.code, 500)
        # The page needs the reason, not a closed connection it cannot explain.
        self.assertIn("disk on fire", json.loads(caught.exception.read())["error"])


class TestGuardEnforcement(Case):
    def test_audit_is_the_default_so_nothing_is_refused(self):
        # blocking() computes what enforcement *would* refuse; config decides
        # whether it is applied. Shipping in audit is what lets the tier
        # boundaries be corrected by real use before they cost anyone anything.
        self.assertFalse(config.enforcing())
        self.assertTrue(guard.blocking(guard.scan("import subprocess"), granted=set()))

    def test_enforcement_blocks_an_ungranted_capability(self):
        findings = guard.scan("import socket")
        self.assertTrue(guard.blocking(findings, granted=set()))

    def test_a_grant_unblocks_its_own_capability_only(self):
        findings = guard.scan("import socket\nimport subprocess")
        blocked = guard.blocking(findings, granted={"network"})
        self.assertEqual({f.name for f in blocked}, {"subprocess"})

    def test_escape_traversal_is_never_grantable(self):
        findings = guard.scan("().__class__.__bases__")
        self.assertTrue(guard.blocking(findings, granted={"network", "process", "dynamic"}))

    def test_refusal_names_both_ways_forward(self):
        text = guard.refusal(guard.scan("import socket"))
        self.assertIn("propose_helper", text)
        self.assertIn("request_capability", text)

    def test_config_round_trip(self):
        config.save({**config.load(), "guard": "enforce"})
        self.assertTrue(config.enforcing())


class TestRunAs(Case):
    """Running the interpreter as a separate OS user.

    This is the only boundary the kernel enforces; everything else in the guard
    is policy that Python's dynamism can defeat.
    """

    def test_no_runner_means_a_plain_spawn(self):
        command, extra = interpreter._spawn_plan(None)
        self.assertEqual(extra, {})
        self.assertNotIn("sudo", command)

    def test_an_unprivileged_parent_goes_through_sudo(self):
        # subprocess(user=) needs CAP_SETUID, so a normal install cannot drop
        # privileges on its own — verified against the real syscall, not assumed.
        if os.getuid() == 0:
            self.skipTest("running as root, which takes the direct path")
        command, extra = interpreter._spawn_plan("nobody")
        self.assertEqual(command[:4], ["sudo", "-n", "-u", "nobody"])
        self.assertEqual(extra, {})

    def test_root_drops_directly_and_sets_the_group_too(self):
        if os.getuid() != 0:
            self.skipTest("needs root to exercise the direct path")
        _, extra = interpreter._spawn_plan("nobody")
        # Passing user without group leaves the gid at root — the classic way a
        # privilege drop turns out to have dropped almost nothing.
        self.assertIn("group", extra)
        self.assertIn("extra_groups", extra)
        self.assertNotEqual(extra["group"], 0)

    def test_sudoers_line_names_the_real_interpreter(self):
        line = interpreter.sudoers_line("codeact-runner")
        self.assertIn("NOPASSWD", line)
        self.assertIn(sys.executable, line)
        self.assertIn("(codeact-runner)", line)

    def test_an_unusable_runner_refuses_rather_than_running_as_you(self):
        # Silently falling back would be worse than not offering the option:
        # someone who set run_as believes they are isolated.
        config.save({**config.load(), "run_as": "no-such-user-exists-here"})
        session = interpreter.Session()
        self.addCleanup(session.stop)
        with self.assertRaises(interpreter.SandboxUnavailable):
            session.start()


class TestSecrets(Case):
    def test_wrapper_redacts_every_way_of_stringifying_it(self):
        s = secrets_store.Secret("TOKEN", "hunter2-abcdefgh")
        self.assertNotIn("hunter2", str(s))
        self.assertNotIn("hunter2", repr(s))
        self.assertNotIn("hunter2", f"{s}")
        self.assertNotIn("hunter2", "{}".format(s))
        self.assertEqual(s.reveal(), "hunter2-abcdefgh")

    def test_egress_redaction_catches_a_traceback(self):
        secrets_store.put("TOKEN", "sk-abcdefghijklmnop")
        leak = "ConnectionError: header Authorization: Bearer sk-abcdefghijklmnop"
        self.assertNotIn("sk-abcdef", secrets_store.redact(leak))

    def test_agent_code_cannot_read_a_secret(self):
        secrets_store.put("TOKEN", "sk-abcdefghijklmnop")
        with self.assertRaises(secrets_store.Denied):
            secrets_store.get("TOKEN", registry_names=set())

    def test_denial_names_the_fix(self):
        secrets_store.put("TOKEN", "sk-abcdefghijklmnop")
        try:
            secrets_store.get("TOKEN", registry_names=set())
        except secrets_store.Denied as exc:
            self.assertIn("requires_secrets", str(exc))

    def test_unknown_secret_is_a_keyerror_naming_the_command(self):
        with self.assertRaises(KeyError) as ctx:
            secrets_store.get("NOPE", registry_names={__name__})
        self.assertIn("secret set NOPE", str(ctx.exception))

    def test_store_file_is_owner_only(self):
        secrets_store.put("TOKEN", "value-goes-here")
        mode = secrets_store.store_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_storage_description_is_honest_about_encryption(self):
        # The stdlib ships no cipher; claiming encryption would be worse than
        # saying so plainly.
        self.assertIn("permissions only", secrets_store.describe_storage())

    def test_short_values_are_not_redacted_from_everything(self):
        secrets_store.put("TINY", "ab")
        self.assertEqual(secrets_store.redact("about"), "about")


class TestMiner(Case):
    def corpus(self, rows):
        path = self.home / "corpus.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_same_shape_different_names_share_a_fingerprint(self):
        a, _ = miner.fingerprint(
            "rows=[]\nfor r in data:\n    if r['x']>1:\n        rows.append(r['y'])\nprint(len(rows))"
        )
        b, _ = miner.fingerprint(
            "out=[]\nfor item in recs:\n    if item['x']>9:\n        out.append(item['y'])\nprint(len(out))"
        )
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_different_shapes_differ(self):
        a, _ = miner.fingerprint("total=0\nfor r in data:\n    total += r['n']\nprint(total)")
        b, _ = miner.fingerprint("out=[]\nfor r in data:\n    out.append(r['n'])\nprint(out)")
        self.assertNotEqual(a, b)

    def test_trivial_blocks_are_not_patterns(self):
        self.assertEqual(miner.fingerprint("x = 1")[0], "")

    def test_syntax_error_does_not_raise(self):
        self.assertEqual(miner.fingerprint("def (:")[0], "")

    def test_a_pattern_in_one_session_is_not_a_candidate(self):
        code = "acc=[]\nfor r in rows:\n    if r['ok']:\n        acc.append(r['v'])\nprint(len(acc))"
        self.corpus([
            {"code": code, "session": "s1", "project": "/p", "outcome": "ok"},
            {"code": code, "session": "s1", "project": "/p", "outcome": "ok"},
        ])
        self.assertEqual(miner.queues(registry.Registry().load())["candidates"], [])

    def test_a_pattern_across_sessions_is_a_candidate(self):
        code = "acc=[]\nfor r in rows:\n    if r['ok']:\n        acc.append(r['v'])\nprint(len(acc))"
        self.corpus([
            {"code": code, "session": "s1", "project": "/p", "outcome": "ok"},
            {"code": code, "session": "s2", "project": "/q", "outcome": "error"},
        ])
        candidates = miner.queues(registry.Registry().load())["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["sessions"], 2)

    def test_cross_project_spread_outranks_raw_frequency(self):
        one = miner.Cluster("a", "", count=9, sessions={"s1"}, projects={"/p"})
        many = miner.Cluster("b", "", count=3, sessions={"s1", "s2", "s3"},
                             projects={"/p", "/q", "/r"})
        self.assertGreater(many.score(), one.score())

    def test_reimplementing_an_existing_helper_is_a_retrieval_failure(self):
        (self.seeds / "shout.py").write_text(GOOD.replace("slugify", "shout"))
        reg = registry.Registry().load()
        # A realistic re-implementation: same calls, different names, and a
        # hardcoded separator where the helper takes a parameter.
        body = (
            "import re\n"
            "parts = [p for p in re.split(r'[^a-zA-Z0-9]+', title) if p]\n"
            "if not parts:\n"
            "    raise ValueError('nothing to slug')\n"
            "'-'.join(p.lower() for p in parts)\n"
        )
        self.corpus([
            {"code": body, "session": "s1", "project": "/p", "outcome": "ok"},
            {"code": body, "session": "s2", "project": "/p", "outcome": "ok"},
        ])
        q = miner.queues(reg)
        self.assertTrue(q["retrieval_failures"], "inline copy should match the helper")
        self.assertEqual(q["retrieval_failures"][0]["helper"], "shout")
        # And it must NOT be offered as a new candidate — that is the whole point.
        self.assertEqual(q["candidates"], [])

    def test_usage_counts_calls_and_failures(self):
        (self.seeds / "shout.py").write_text(GOOD.replace("slugify", "shout"))
        reg = registry.Registry().load()
        self.corpus([
            {"code": "shout('a')", "session": "s1", "project": "/p", "outcome": "ok"},
            {"code": "shout('b')", "session": "s1", "project": "/q", "outcome": "error"},
        ])
        stats = miner.usage(reg, cached=False)["shout"]
        self.assertEqual((stats["calls"], stats["failures"]), (2, 1))
        self.assertEqual(len(stats["projects"]), 2)


class TestTrialRun(Case):
    def test_a_clean_candidate_reports_no_reach(self):
        report = trial.run(GOOD, [{"code": "slugify('Hi There')"}])
        self.assertEqual(report["fatal"], "")
        self.assertTrue(report["results"][0]["ok"])
        self.assertFalse(report["effects"].get("network"))

    def test_reach_is_surfaced_to_the_reviewer(self):
        reaching = '''from codeact import helper
@helper(job="acquire", domains=["http"], side_effects="network",
        examples=[{"code": "peek()"}])
def peek() -> str:
    """Look up a host to demonstrate the effect report.

    Use when: demonstrating this. Don't use when: anything real.
    Returns:
        a short string saying it finished
    """
    import socket, subprocess, sys
    socket.getaddrinfo("example.com", 80)
    subprocess.run([sys.executable, "-c", "pass"])
    return "done"
'''
        report = trial.run(reaching, [{"code": "peek()"}])
        self.assertTrue(report["effects"].get("network"))
        self.assertTrue(report["effects"].get("processes"))
        lines = " ".join(trial.summarize_effects(report["effects"]))
        self.assertIn("contacted", lines)
        self.assertIn("spawned", lines)

    def test_a_trial_gets_no_real_credentials(self):
        secrets_store.put("TOKEN", "sk-abcdefghijklmnop")
        peeking = '''from codeact import helper
@helper(job="inspect", domains=["fs"], examples=[{"code": "peek()"}])
def peek() -> str:
    """Report whether the environment carries anything.

    Use when: demonstrating. Don't use when: anything real.
    Returns:
        a string listing what leaked
    """
    import os
    return ",".join(k for k in os.environ if "CODEACT" in k or "TOKEN" in k)
'''
        report = trial.run(peeking, [{"code": "peek()"}])
        self.assertNotIn("sk-abcdef", json.dumps(report))
        self.assertNotIn("CODEACT_SECRETS", report["results"][0]["output"])

    def test_a_hanging_candidate_is_killed(self):
        slow = '''from codeact import helper
@helper(job="transform", domains=["data"], examples=[{"code": "wait()"}])
def wait() -> int:
    """Sleep for a long time to prove the timeout works.

    Use when: testing. Don't use when: anything real.
    Returns:
        an integer that never arrives
    """
    import time
    time.sleep(60)
    return 1
'''
        report = trial.run(slow, [{"code": "wait()"}], timeout=3.0)
        self.assertIn("timed out", report["fatal"])

    def test_source_that_explodes_on_import_is_reported(self):
        report = trial.run("raise RuntimeError('boom')", [{"code": "x"}])
        self.assertIn("RuntimeError", report["fatal"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
