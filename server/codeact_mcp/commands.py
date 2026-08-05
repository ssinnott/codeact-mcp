"""Capability policy, layer 0: shell commands that belong in CodeAct instead.

The guard (§9) decides what Python may do once it is already inside the
interpreter. This decides something earlier and coarser: which commands the
agent may run in `Bash` at all, and which have to arrive as a reviewed helper.

The motivating case is `kubectl`. Against a local cluster it is a normal
development tool and blocking it would be pure friction. Against a real cluster
it is a loaded gun with no undo, and the useful boundary is not "read vs write"
in the abstract but *which cluster you are pointed at*. So the rule is:

    local target  → Bash, unrestricted
    other target  → CodeAct, read-only

Two enforcement points share the classification below, because they know
different things:

* The `Bash` PreToolUse hook runs as its own process and can afford to ask
  `kubectl config current-context` what the command would actually hit, so it
  resolves the implicit target and blocks anything non-local outright.
* Inside the interpreter an audit hook cannot resolve anything — spawning
  `kubectl` from within an audit hook that fires on spawning would recurse — so
  it works lexically instead: read-only verbs pass against any target, and a
  mutating verb passes only when the argv itself names a local context.

That asymmetry is deliberate rather than an oversight. It also produces the
right habit: state-changing work in code says out loud what it is aimed at.

**This is a policy layer, not a security boundary** — the same disclaimer the
AST guard carries, and for the same reason. The parser below understands
pipelines, `sudo`, `env`, `xargs`, and `sh -c`, and it fails closed on anything
it cannot parse, but a shell is an interpreter and code that wants to hide a
command from it can. It exists to keep an honest agent out of production, and
to make the alternative route obvious when it says no.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import paths

# The two callers, which get different answers to the same question.
BASH = "bash"
CODEACT = "codeact"

MODES = ("off", "audit", "ask", "enforce")

# What `commands.rules` contains when nobody has said otherwise. Inert until
# the mode moves off `off`.
DEFAULT_RULES = ("kubectl", "helm")

# ---------------------------------------------------------------------------
# Command families
#
# A family is a set of commands that share a notion of "what am I pointed at":
# every kube-family tool reads the same kubeconfig, so one resolver serves all
# of them. Commands outside a family have no target to classify, so they are
# simply Bash-blocked and left to helper review.

_FAMILY_OF = {
    "kubectl": "kube",
    "oc": "kube",
    "helm": "kube",
    "kubens": "kube",
    "kubectx": "kube",
    "stern": "kube",
    "k9s": "kube",
}

# Contexts treated as a local development cluster. Names, not endpoints: a
# context name is what the agent and the human both actually see, and reading
# the server URL out of the kubeconfig would need a YAML parser we don't have.
LOCAL_CONTEXTS = (
    "minikube",
    "docker-desktop",
    "docker-for-desktop",
    "rancher-desktop",
    "microk8s",
    "colima",
    "orbstack",
    "kind-*",
    "k3d-*",
    "k3s-*",
    "*-local",
    "local-*",
    "localhost*",
)

# Verbs that only read. Anything not listed is treated as mutating, so a new
# kubectl verb fails closed rather than silently gaining write access.
_READ_ONLY = {
    "kubectl": """
        api-resources api-versions auth cluster-info describe diff events
        explain get logs top wait
    """,
    "oc": """
        api-resources api-versions auth cluster-info describe diff events
        explain get logs status top wait whoami
    """,
    "helm": "get history list ls search show status verify",
    "stern": "",  # every invocation tails logs
    "kubens": "",
    "kubectx": "",
}

# Subcommands that never reach a cluster at all — they read or edit local
# files. Allowed everywhere, regardless of which context is current, because
# refusing `kubectl config use-context` would block the documented way out of a
# refusal.
_LOCAL_ONLY = {
    "kubectl": "config version completion options help",
    "oc": "config version completion options help",
    "helm": "version env help completion create lint template repo plugin",
}

# Flags carrying a value in the next token, needed both to find the subcommand
# and to spot an explicit target. `--flag=value` needs no entry.
_VALUE_FLAGS = frozenset(
    """
    -n --namespace --context --kube-context --kubeconfig --cluster --user --as
    --as-group --token --server -s --request-timeout --cache-dir --log-file
    --certificate-authority --client-certificate --client-key -o --output
    -l --selector -f --filename -c --container --field-selector -v --v
    --kube-apiserver --kube-token --kube-ca-file
    """.split()
)

_CONTEXT_FLAGS = ("--context", "--kube-context")
# Both are accepted everywhere above; this is only which one to suggest.
_CONTEXT_FLAG_FOR = {"helm": "--kube-context"}
_KUBECONFIG_FLAGS = ("--kubeconfig", "--kube-apiserver", "--server")


# ---------------------------------------------------------------------------
# Parsing a command line into the commands it would actually run


@dataclass(frozen=True)
class Command:
    """One simple command from a shell line, with its wrappers stripped."""

    name: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


class Unparseable(ValueError):
    """The line is not something we can take apart — treated as suspicious."""


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"^\d*(>>|>&|&>|<<|>|<)$")
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Commands that run another command. Unwrapped rather than matched, so
# `sudo kubectl delete` is a kubectl invocation and not a sudo one.
_WRAPPERS = frozenset(
    """
    sudo doas env command time timeout watch nohup xargs nice ionice stdbuf
    setsid script parallel
    """.split()
)
_WRAPPER_VALUE_FLAGS = frozenset(
    "-u --user -g --group -n --interval -s --signal -k --kill-after -I -i -P -a".split()
)
_SHELLS = frozenset("sh bash zsh dash ksh busybox".split())

_MAX_DEPTH = 3  # `sh -c "sh -c ..."` terminates somewhere


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:  # unbalanced quote, unterminated substitution
        raise Unparseable(str(exc)) from exc


def _segments(tokens: Sequence[str]) -> list[list[str]]:
    """Split on shell operators. Redirection targets are dropped, not run."""
    out: list[list[str]] = []
    current: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and all(ch in ";&|()" for ch in token):
            if current:
                out.append(current)
                current = []
        elif _REDIRECT.match(token):
            index += 1  # and skip the file it points at
        else:
            current.append(token)
        index += 1
    if current:
        out.append(current)
    return out


def _unwrap(tokens: Sequence[str], depth: int) -> list[Command]:
    env: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            key, _, value = token.partition("=")
            env[key] = value
            index += 1
            continue

        name = os.path.basename(token)

        if name in _WRAPPERS:
            index += 1
            while index < len(tokens):
                candidate = tokens[index]
                if _ASSIGNMENT.match(candidate):
                    key, _, value = candidate.partition("=")
                    env[key] = value
                    index += 1
                elif candidate.startswith("-"):
                    index += 2 if candidate in _WRAPPER_VALUE_FLAGS else 1
                elif name in ("timeout", "watch") and _DURATION.match(candidate):
                    index += 1
                else:
                    break
            continue

        if name in _SHELLS and depth < _MAX_DEPTH:
            for offset in range(index + 1, len(tokens)):
                if tokens[offset] in ("-c", "-lc") and offset + 1 < len(tokens):
                    nested = parse(tokens[offset + 1], depth + 1)
                    for command in nested:
                        command.env.update(
                            {k: v for k, v in env.items() if k not in command.env}
                        )
                    return nested

        return [Command(name=name, argv=tuple(tokens[index:]), env=env)]

    return []


def parse(line: str, depth: int = 0) -> list[Command]:
    """Every command the line would run, wrappers and `sh -c` payloads included.

    Raises `Unparseable` when the line cannot be tokenized, which callers treat
    as a reason to refuse rather than a reason to shrug.
    """
    commands: list[Command] = []
    for segment in _segments(_tokens(line)):
        commands.extend(_unwrap(segment, depth))
    return commands


def from_argv(argv: Iterable[object], depth: int = 0) -> list[Command]:
    """Same, for an already-split argv — what a spawn audit hook receives."""
    tokens = []
    for item in argv:
        try:
            tokens.append(os.fsdecode(item))
        except TypeError:
            tokens.append(str(item))
    return _unwrap(tokens, depth)


# ---------------------------------------------------------------------------
# Reading a command


def subcommand(argv: Sequence[str]) -> str | None:
    """The first non-flag argument: `get` in `kubectl -n prod get pods`."""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            index += 2 if token in _VALUE_FLAGS and "=" not in token else 1
            continue
        return token
    return None


def explicit_context(argv: Sequence[str]) -> str | None:
    """A context named in the argv itself, in either flag spelling."""
    for index, token in enumerate(argv):
        for flag in _CONTEXT_FLAGS:
            if token == flag and index + 1 < len(argv):
                return argv[index + 1]
            if token.startswith(flag + "="):
                return token.split("=", 1)[1]
    return None


def redirects_config(command: Command) -> bool:
    """Points somewhere we cannot classify — a foreign kubeconfig or server."""
    if "KUBECONFIG" in command.env:
        return True
    for token in command.argv:
        for flag in _KUBECONFIG_FLAGS:
            if token == flag or token.startswith(flag + "="):
                return True
    return False


def resolve_kube_context() -> str | None:
    """Ask kubectl what the current context is.

    Only safe to call from a process that is *not* running the interpreter's
    spawn audit hook — see the module docstring. Returns None when kubectl is
    absent, unconfigured, or slow, and None means "unknown", which callers
    treat as non-local.
    """
    try:
        done = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.strip()
    return name if done.returncode == 0 and name else None


def is_local(context: str | None, patterns: Sequence[str]) -> bool:
    if not context:
        return False
    lowered = context.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


# ---------------------------------------------------------------------------
# Rules and decisions


@dataclass(frozen=True)
class Rule:
    command: str
    family: str | None
    local: tuple[str, ...]
    always_allow: frozenset[str]
    read_only: frozenset[str]
    guidance: str = ""

    @property
    def classifies_reads(self) -> bool:
        """Whether we know this command's read-only verbs.

        Without that vocabulary the interpreter has nothing to enforce, so the
        command is Bash-blocked and read-only-ness is left to helper review.
        """
        return self.family is not None

    @classmethod
    def build(cls, spec: str | dict) -> "Rule":
        if isinstance(spec, str):
            spec = {"command": spec}
        name = str(spec.get("command") or "").strip()
        if not name:
            raise ValueError("a command rule needs a `command`")
        family = spec.get("family", _FAMILY_OF.get(name))
        read_only = spec.get("read_only", _READ_ONLY.get(name, ""))
        always = spec.get("always_allow", _LOCAL_ONLY.get(name, ""))
        return cls(
            command=name,
            family=family,
            local=tuple(spec.get("local") or LOCAL_CONTEXTS),
            always_allow=frozenset(_words(always)),
            read_only=frozenset(_words(read_only)),
            guidance=str(spec.get("guidance") or ""),
        )


def _words(value: str | Iterable[str]) -> list[str]:
    return value.split() if isinstance(value, str) else [str(v) for v in value]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    command: str | None = None
    reason: str = ""
    message: str = ""
    target: str | None = None


_ALLOWED = Decision(allowed=True)


class Policy:
    def __init__(self, mode: str, rules: Sequence[Rule]) -> None:
        self.mode = mode if mode in MODES else "off"
        self.rules = {rule.command: rule for rule in rules}

    @classmethod
    def load(cls, settings: dict | None = None) -> "Policy":
        if settings is None:
            from . import config

            settings = config.load()
        raw = settings.get("commands") or {}
        if not isinstance(raw, dict):
            raw = {}
        rules = []
        for spec in raw.get("rules") or DEFAULT_RULES:
            try:
                rules.append(Rule.build(spec))
            except (ValueError, TypeError, AttributeError):
                continue  # a malformed rule must not disarm the rest
        return cls(str(raw.get("mode", "off")).lower(), rules)

    @property
    def active(self) -> bool:
        return self.mode != "off" and bool(self.rules)

    @property
    def blocks_in_codeact(self) -> bool:
        """`audit` observes Bash but never interferes with running helpers."""
        return self.mode in ("ask", "enforce") and bool(self.rules)

    # -- the check ---------------------------------------------------------

    def check_line(
        self,
        line: str,
        *,
        surface: str = BASH,
        resolve: Callable[[], str | None] | None = None,
    ) -> Decision:
        try:
            commands = parse(line)
        except Unparseable as exc:
            return self._unparseable(line, str(exc))
        return self.check_commands(commands, surface=surface, resolve=resolve)

    def check_argv(
        self,
        argv: Iterable[object],
        *,
        surface: str = CODEACT,
        resolve: Callable[[], str | None] | None = None,
    ) -> Decision:
        return self.check_commands(from_argv(argv), surface=surface, resolve=resolve)

    def check_commands(
        self,
        commands: Sequence[Command],
        *,
        surface: str,
        resolve: Callable[[], str | None] | None = None,
    ) -> Decision:
        matched: Decision | None = None
        for command in commands:
            decision = self._check_one(command, surface=surface, resolve=resolve)
            if not decision.allowed:
                return decision  # first refusal wins; one is enough to stop
            if decision.command and matched is None:
                matched = decision
        return matched or _ALLOWED

    def _check_one(
        self,
        command: Command,
        *,
        surface: str,
        resolve: Callable[[], str | None] | None,
    ) -> Decision:
        rule = self.rules.get(command.name)
        if rule is None:
            return _ALLOWED

        sub = subcommand(command.argv)
        if sub and sub in rule.always_allow:
            return Decision(True, rule.command, "touches no cluster")

        if not rule.classifies_reads:
            # No target to classify, so there is nothing to allow in Bash.
            if surface == CODEACT:
                # Getting in here meant passing through a human-reviewed helper.
                return Decision(True, rule.command, "left to helper review")
            return Decision(
                False, rule.command, "not allowed in Bash", _refuse_in_bash(rule, command, None)
            )

        context = explicit_context(command.argv)
        named = context is not None
        if context is None and not redirects_config(command) and resolve is not None:
            context = resolve()
        local = is_local(context, rule.local)

        if surface == CODEACT:
            if sub and sub in rule.read_only:
                return Decision(True, rule.command, "read-only", target=context)
            if local:
                return Decision(True, rule.command, "local target", target=context)
            return Decision(
                False,
                rule.command,
                "mutating verb, target not known to be local",
                _refuse_in_codeact(rule, command, sub, named),
                context,
            )

        if local:
            return Decision(True, rule.command, "local target", target=context)
        return Decision(
            False,
            rule.command,
            "target is not a local cluster",
            _refuse_in_bash(rule, command, context),
            context,
        )

    def _unparseable(self, line: str, why: str) -> Decision:
        """Fail closed, but only for lines that mention a governed command."""
        for name, rule in self.rules.items():
            if re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])", line):
                return Decision(
                    False,
                    name,
                    f"unparseable command line ({why})",
                    f"BLOCKED — `{name}` appears in a command line this policy could "
                    f"not parse ({why}), so it cannot be shown to target a local "
                    "cluster.\n\nRewrite it as a plain command, or do the work in "
                    "CodeAct — see `search_helpers`.",
                )
        return _ALLOWED


# ---------------------------------------------------------------------------
# Refusals
#
# Same shape as the guard's: say what was refused, say why, and name the route
# that does work. A refusal with no route is just a broken tool.


# Verbs worth naming first when suggesting a read. Alphabetical order would
# lead with `api-resources`, which is nobody's idea of the obvious alternative.
_SUGGEST_FIRST = ("get", "list", "describe", "logs", "status", "history", "top", "events")


def _suggest_reads(rule: Rule, limit: int = 5) -> str:
    preferred = [v for v in _SUGGEST_FIRST if v in rule.read_only]
    rest = sorted(rule.read_only - set(preferred))
    return ", ".join((preferred + rest)[:limit])


def _codeact_routes(rule: Rule) -> list[str]:
    return [
        '  1. search_helpers(task="…what you need to read…", job="inspect") — then '
        "call it from run_python.",
        "  2. propose_helper(...) if nothing fits. Package the read you need; a helper "
        "that changes state somewhere real will not be approved.",
    ]


def _refuse_in_bash(rule: Rule, command: Command, context: str | None) -> str:
    lines = []
    if not rule.classifies_reads:
        lines.append(f"BLOCKED — `{rule.command}` runs through CodeAct, not Bash.")
        lines.append("")
        lines.append("Two ways forward:")
        lines.extend(_codeact_routes(rule))
        if rule.guidance:
            lines += ["", rule.guidance]
        return "\n".join(lines)

    if context:
        lines.append(
            f"BLOCKED — `{rule.command}` here targets `{context}`, which is not a "
            "local cluster."
        )
    else:
        lines.append(
            f"BLOCKED — could not determine which cluster `{rule.command}` would "
            "target, so it is treated as a real one."
        )
        if redirects_config(command):
            lines.append(
                "(The command sets its own kubeconfig or server, so the current "
                "context says nothing about where it points.)"
            )

    lines.append("")
    lines.append(
        "Bash may target local clusters only ("
        + ", ".join(rule.local[:4])
        + ", …). Anything else goes through CodeAct, where reads are allowed and "
        "writes are not:"
    )
    lines.extend(_codeact_routes(rule))
    lines.append("")
    lines.append(
        "If you meant the local cluster, switch to it first — "
        "`kubectl config use-context <local>` — or name it inline with "
        f"`{_CONTEXT_FLAG_FOR.get(rule.command, '--context')}`."
    )
    if rule.guidance:
        lines.append("")
        lines.append(rule.guidance)
    return "\n".join(lines)


def _refuse_in_codeact(
    rule: Rule, command: Command, sub: str | None, named: bool
) -> str:
    verb = f"{rule.command} {sub}" if sub else rule.command
    lines = [
        f"BLOCKED — `{verb}` changes state, and in CodeAct that is allowed only "
        "against a target shown to be local.",
        "",
    ]
    if named:
        lines.append(
            f"`{explicit_context(command.argv)}` does not match any local context "
            "pattern (" + ", ".join(rule.local[:4]) + ", …)."
        )
    else:
        lines.append(
            "No context is named in the command. In here the target has to be "
            "explicit — the interpreter deliberately cannot go and ask which "
            "context is current, so an unnamed one counts as real."
        )
    lines += [
        "",
        "Three ways forward:",
        f"  1. Read instead — `{rule.command} {_suggest_reads(rule)}`, … all work "
        "against any target.",
        f"  2. Name the context if it is local: "
        f"`{_CONTEXT_FLAG_FOR.get(rule.command, '--context')} kind-…` makes this a "
        "local operation and it runs.",
        "  3. Run it in Bash against your local cluster, where it isn't blocked.",
    ]
    if rule.guidance:
        lines += ["", rule.guidance]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Audit log


def record(decision: Decision, line: str, *, surface: str, mode: str) -> None:
    """Best effort — the log must never be why a command fails."""
    if decision.command is None:
        return
    try:
        from . import corpus

        paths.ensure()
        entry = {
            "ts": time.time(),
            "project": os.getcwd(),
            "surface": surface,
            "mode": mode,
            "command": decision.command,
            "line": corpus.redact(line)[:2000],
            "allowed": decision.allowed,
            "reason": decision.reason,
            "target": decision.target,
        }
        with (paths.root() / "commands.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
