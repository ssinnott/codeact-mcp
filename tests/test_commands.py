"""Layer 0: which commands belong in Bash, and which have to go through CodeAct."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from codeact_mcp import commands, interpreter  # noqa: E402

KUBE = {"commands": {"mode": "enforce", "rules": ["kubectl", "helm"]}}
LOCAL = lambda: "kind-dev"  # noqa: E731
REMOTE = lambda: "prod-eks"  # noqa: E731
UNKNOWN = lambda: None  # noqa: E731


def names(line):
    return [c.name for c in commands.parse(line)]


class Parsing(unittest.TestCase):
    def test_pipelines_and_chains_are_separate_commands(self):
        self.assertEqual(
            names("kubectl get pods | grep web && echo done; ls"),
            ["kubectl", "grep", "echo", "ls"],
        )

    def test_env_prefix_is_not_the_command(self):
        [cmd] = commands.parse("KUBECONFIG=/tmp/kc kubectl get pods")
        self.assertEqual(cmd.name, "kubectl")
        self.assertEqual(cmd.env["KUBECONFIG"], "/tmp/kc")

    def test_wrappers_are_unwrapped(self):
        self.assertEqual(names("sudo -u root kubectl delete pod x"), ["kubectl"])
        self.assertEqual(names("timeout 30 kubectl get pods"), ["kubectl"])
        self.assertEqual(names("env FOO=1 kubectl get pods"), ["kubectl"])
        self.assertEqual(names("cat list | xargs -n1 kubectl delete pod"), ["cat", "kubectl"])

    def test_shell_payloads_are_looked_inside(self):
        self.assertEqual(names("bash -c 'kubectl get pods | wc -l'"), ["kubectl", "wc"])

    def test_absolute_paths_resolve_to_the_binary_name(self):
        self.assertEqual(names("/usr/local/bin/kubectl get pods"), ["kubectl"])

    def test_redirection_targets_are_not_commands(self):
        self.assertEqual(names("kubectl get pods > kubectl.txt"), ["kubectl"])

    def test_command_substitution_is_reached(self):
        self.assertIn("kubectl", names("echo $(kubectl get pods)"))

    def test_unbalanced_quotes_are_refused_not_ignored(self):
        with self.assertRaises(commands.Unparseable):
            commands.parse("kubectl get pods 'unterminated")


class Reading(unittest.TestCase):
    def test_subcommand_skips_flags_and_their_values(self):
        argv = ("kubectl", "-n", "prod", "--context", "x", "get", "pods")
        self.assertEqual(commands.subcommand(argv), "get")

    def test_subcommand_handles_joined_flag_values(self):
        self.assertEqual(
            commands.subcommand(("kubectl", "--context=x", "delete", "pod")), "delete"
        )

    def test_explicit_context_in_both_spellings(self):
        self.assertEqual(
            commands.explicit_context(("kubectl", "--context", "a", "get")), "a"
        )
        self.assertEqual(
            commands.explicit_context(("helm", "--kube-context=b", "list")), "b"
        )
        self.assertIsNone(commands.explicit_context(("kubectl", "get", "pods")))

    def test_local_patterns_match_by_name(self):
        for context in ("kind-dev", "minikube", "docker-desktop", "K3D-Test", "app-local"):
            self.assertTrue(commands.is_local(context, commands.LOCAL_CONTEXTS), context)
        for context in ("prod-eks", "staging", "default", None):
            self.assertFalse(commands.is_local(context, commands.LOCAL_CONTEXTS), context)


class InBash(unittest.TestCase):
    def setUp(self):
        self.policy = commands.Policy.load(KUBE)

    def decide(self, line, resolve=REMOTE):
        return self.policy.check_line(line, surface=commands.BASH, resolve=resolve)

    def test_local_cluster_is_left_alone(self):
        self.assertTrue(self.decide("kubectl delete pod web", resolve=LOCAL).allowed)

    def test_real_cluster_is_blocked_even_for_a_read(self):
        decision = self.decide("kubectl get pods")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.target, "prod-eks")
        self.assertIn("search_helpers", decision.message)
        self.assertIn("propose_helper", decision.message)

    def test_unknown_target_is_treated_as_real(self):
        self.assertFalse(self.decide("kubectl get pods", resolve=UNKNOWN).allowed)

    def test_explicit_context_beats_the_current_one(self):
        self.assertTrue(self.decide("kubectl --context kind-dev get pods").allowed)
        self.assertFalse(self.decide("kubectl --context prod get pods", resolve=LOCAL).allowed)

    def test_own_kubeconfig_is_unclassifiable_so_blocked(self):
        self.assertFalse(self.decide("KUBECONFIG=/tmp/kc kubectl get pods", resolve=LOCAL).allowed)
        self.assertFalse(self.decide("kubectl --kubeconfig /tmp/kc get pods", resolve=LOCAL).allowed)

    def test_config_subcommands_never_touch_a_cluster(self):
        self.assertTrue(self.decide("kubectl config use-context prod").allowed)
        self.assertTrue(self.decide("kubectl version --client").allowed)

    def test_a_blocked_command_anywhere_in_the_line_stops_it(self):
        self.assertFalse(self.decide("echo hi && kubectl get pods").allowed)
        self.assertFalse(self.decide("sudo kubectl drain node-1").allowed)

    def test_other_commands_are_none_of_our_business(self):
        self.assertTrue(self.decide("ls -la && git status").allowed)

    def test_unparseable_lines_fail_closed_only_when_relevant(self):
        self.assertFalse(self.decide("kubectl get pods 'x").allowed)
        self.assertTrue(self.decide("echo 'unterminated").allowed)
        self.assertTrue(self.decide("cat kubectl-notes.md 'x").allowed)

    def test_helm_shares_the_kube_family(self):
        self.assertFalse(self.decide("helm upgrade api ./chart").allowed)
        self.assertTrue(self.decide("helm upgrade api ./chart", resolve=LOCAL).allowed)


class InCodeact(unittest.TestCase):
    def setUp(self):
        self.policy = commands.Policy.load(KUBE)

    def decide(self, argv):
        return self.policy.check_argv(argv, surface=commands.CODEACT)

    def test_reads_work_against_anything(self):
        self.assertTrue(self.decide(["kubectl", "get", "pods", "-o", "json"]).allowed)
        self.assertTrue(self.decide(["kubectl", "logs", "web-0"]).allowed)
        self.assertTrue(self.decide(["helm", "list", "-A"]).allowed)

    def test_writes_need_a_named_local_context(self):
        decision = self.decide(["kubectl", "delete", "pod", "web-0"])
        self.assertFalse(decision.allowed)
        self.assertIn("kubectl delete", decision.message)
        self.assertTrue(
            self.decide(["kubectl", "--context", "kind-dev", "delete", "pod", "web-0"]).allowed
        )
        self.assertFalse(
            self.decide(["kubectl", "--context", "prod-eks", "delete", "pod", "web-0"]).allowed
        )

    def test_unknown_verbs_are_treated_as_writes(self):
        self.assertFalse(self.decide(["kubectl", "rollout", "restart", "deploy/web"]).allowed)

    def test_shell_form_is_taken_apart_too(self):
        decision = self.policy.check_argv(
            ["/bin/sh", "-c", "kubectl delete pod web-0"], surface=commands.CODEACT
        )
        self.assertFalse(decision.allowed)

    def test_the_interpreter_never_resolves_a_context(self):
        # No resolver is passed here on purpose: see commands.py's docstring.
        self.assertFalse(self.decide(["kubectl", "apply", "-f", "x.yaml"]).allowed)


class Rules(unittest.TestCase):
    def test_a_command_with_no_family_is_bash_blocked_only(self):
        policy = commands.Policy.load({"commands": {"mode": "enforce", "rules": ["terraform"]}})
        bash = policy.check_line("terraform apply", surface=commands.BASH)
        self.assertFalse(bash.allowed)
        self.assertIn("CodeAct", bash.message)
        # In the interpreter it already passed helper review, so it runs.
        self.assertTrue(policy.check_argv(["terraform", "apply"], surface=commands.CODEACT).allowed)

    def test_rules_can_be_customized(self):
        policy = commands.Policy.load(
            {
                "commands": {
                    "mode": "enforce",
                    "rules": [{"command": "kubectl", "local": ["dev-*"], "guidance": "ask ops"}],
                }
            }
        )
        self.assertTrue(
            policy.check_line("kubectl get pods", surface=commands.BASH, resolve=lambda: "dev-1").allowed
        )
        decision = policy.check_line(
            "kubectl get pods", surface=commands.BASH, resolve=lambda: "minikube"
        )
        self.assertFalse(decision.allowed)
        self.assertIn("ask ops", decision.message)

    def test_a_malformed_rule_does_not_disarm_the_others(self):
        policy = commands.Policy.load(
            {"commands": {"mode": "enforce", "rules": [{"nope": 1}, "kubectl"]}}
        )
        self.assertIn("kubectl", policy.rules)

    def test_off_by_default(self):
        self.assertFalse(commands.Policy.load({}).active)
        self.assertFalse(commands.Policy.load({"commands": {"mode": "off"}}).active)

    def test_audit_mode_watches_bash_but_leaves_the_interpreter_alone(self):
        policy = commands.Policy.load({"commands": {"mode": "audit", "rules": ["kubectl"]}})
        self.assertTrue(policy.active)
        self.assertFalse(policy.blocks_in_codeact)


class InTheInterpreter(unittest.TestCase):
    """The audit hook, end to end through a real session.

    `faketl` doesn't exist, which is the point: a refusal has to arrive before
    the spawn, and the allowed case reaching FileNotFoundError proves the
    policy let it through rather than that the policy was never installed.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="codeact-policy-")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "config.json").write_text(
            json.dumps(
                {
                    "commands": {
                        "mode": "enforce",
                        "rules": [{"command": "faketl", "family": "kube",
                                   "read_only": "get", "local": ["kind-*"]}],
                    }
                }
            )
        )
        previous = os.environ.get("CODEACT_HOME")
        os.environ["CODEACT_HOME"] = str(home)
        self.addCleanup(
            lambda: os.environ.__setitem__("CODEACT_HOME", previous)
            if previous is not None
            else os.environ.pop("CODEACT_HOME", None)
        )
        self.session = interpreter.Session()
        self.session.start()
        self.addCleanup(self.session.stop)
        self.session.execute("import subprocess", 20)

    def run_code(self, code):
        return self.session.execute(code, 20) or {}

    def test_a_write_is_refused_before_it_spawns(self):
        result = self.run_code("subprocess.run(['faketl', 'delete', 'thing'])")
        self.assertIn("PermissionError", result["error"])
        self.assertIn("BLOCKED", result["error"])

    def test_shell_true_is_refused_too(self):
        result = self.run_code("subprocess.run('faketl delete thing', shell=True)")
        self.assertIn("PermissionError", result["error"])

    def test_a_read_is_allowed_through(self):
        result = self.run_code("subprocess.run(['faketl', 'get', 'things'])")
        self.assertIn("FileNotFoundError", result["error"])

    def test_a_write_to_a_named_local_target_is_allowed_through(self):
        result = self.run_code(
            "subprocess.run(['faketl', '--context', 'kind-dev', 'delete', 'thing'])"
        )
        self.assertIn("FileNotFoundError", result["error"])

    def test_everything_else_is_untouched(self):
        result = self.run_code(
            "import sys\n"
            "subprocess.run([sys.executable, '-c', 'print(2)'],"
            " capture_output=True, text=True).stdout"
        )
        self.assertIsNone(result["error"])
        self.assertIn("2", result["result"])


if __name__ == "__main__":
    unittest.main()
