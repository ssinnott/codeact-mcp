"""The interpreter subprocess.

Runs as its own process so a `sys.exit()`, a segfault, or a runaway loop kills
this and not the MCP server. Speaks JSON-lines with the parent.

Protocol integrity matters here: anything the user's code writes to fd 1 would
corrupt the stream. So at startup we dup the real stdout aside for protocol use
and point fd 1 at /dev/null. Subprocesses spawned by executed code inherit the
harmless fd, and `print` is captured separately via redirect_stdout.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import pathlib
import sys
import traceback
import types

MAX_OUTPUT = 4000  # chars returned inline; the full text stays as `_out`
MAX_REPR = 120


def _short(value: object) -> str:
    try:
        text = repr(value)
    except Exception:
        return "<unreprable>"
    text = " ".join(text.split())
    return text if len(text) <= MAX_REPR else text[: MAX_REPR - 1] + "…"


def _describe(value: object) -> str:
    """A summary, not a dump.

    The whole point of keeping large intermediates in the interpreter is that
    they never reach the context window, so reporting a thousand-element list by
    printing its first thousand elements would defeat the exercise.
    """
    if isinstance(value, (str, bytes)):
        if len(value) > 60:
            unit = "chars" if isinstance(value, str) else "bytes"
            return f"{len(value)} {unit}"
        return _short(value)
    if isinstance(value, (list, tuple, set, frozenset, dict, bytearray)):
        try:
            if len(value) > 6:
                return f"{len(value)} items"
        except TypeError:
            pass
        return _short(value)
    if isinstance(value, type):
        return f"class {value.__name__}"
    if callable(value):
        return getattr(value, "__name__", None) or _short(value)
    return _short(value)


def _is_noise(name: str, value: object) -> bool:
    """Names that would clutter the delta without informing anything.

    Imported modules and the REPL result variable are both already visible —
    the first from the code that was just run, the second from the `→` line.
    """
    return (
        name.startswith("__")
        or name in ("_", "_out", "helpers")
        or isinstance(value, types.ModuleType)
        or getattr(value, "__codeact__", None) is not None
    )


def _snapshot(ns: dict) -> dict[str, int]:
    return {k: id(v) for k, v in ns.items() if not k.startswith("__")}


def _delta(before: dict[str, int], ns: dict) -> list[dict[str, str]]:
    """New or rebound names, so the agent can track state without printing it."""
    out = []
    for name, value in ns.items():
        if _is_noise(name, value):
            continue
        prev = before.get(name)
        if prev is None:
            kind = "new"
        elif prev != id(value):
            kind = "changed"
        else:
            continue
        out.append(
            {
                "name": name,
                "kind": kind,
                "type": type(value).__name__,
                "repr": _describe(value),
            }
        )
    out.sort(key=lambda d: d["name"])
    return out


def _split_last_expression(code: str):
    """Compile like a REPL: the trailing expression's value is reported."""
    tree = ast.parse(code)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        head = ast.Module(body=tree.body[:-1], type_ignores=[])
        tail = ast.Expression(body=tree.body[-1].value)
        return compile(head, "<codeact>", "exec"), compile(tail, "<codeact>", "eval")
    return compile(tree, "<codeact>", "exec"), None


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT:
        return text, False
    keep = MAX_OUTPUT // 2
    return text[:keep] + "\n…[truncated, full text in `_out`]…\n" + text[-keep:], True


def execute(ns: dict, code: str) -> dict:
    stdout, stderr = io.StringIO(), io.StringIO()
    before = _snapshot(ns)
    result_repr = None
    result_truncated = False
    error = None

    try:
        head, tail = _split_last_expression(code)
    except SyntaxError:
        return {
            "stdout": "",
            "stderr": "",
            "error": "".join(traceback.format_exception_only(*sys.exc_info()[:2])).strip(),
            "delta": [],
            "result": None,
            "truncated": False,
        }

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(head, ns)
            if tail is not None:
                value = eval(tail, ns)
                if value is not None:
                    ns["_"] = value
                    # The agent asked for this value explicitly, so it gets the
                    # generous output budget rather than the compact one used
                    # for the state delta — truncating a helpers.search() to
                    # 120 characters throws away the answer.
                    #
                    # repr, per REPL convention, except for multi-line strings
                    # where it would escape every newline into unreadable noise.
                    multiline = isinstance(value, str) and "\n" in value
                    result_repr, result_truncated = _truncate(
                        value if multiline else repr(value)
                    )
    except KeyboardInterrupt:
        error = "KeyboardInterrupt: execution exceeded its timeout (state preserved)"
    except SystemExit as exc:
        error = f"SystemExit: {exc.code} (ignored; the session is still alive)"
    except BaseException:
        # Drop our own frame so the traceback starts at the user's code.
        exc_type, exc_value, tb = sys.exc_info()
        error = "".join(traceback.format_exception(exc_type, exc_value, tb.tb_next or tb))

    out_text = stdout.getvalue()
    err_text = stderr.getvalue()
    full = out_text + (("\n" + err_text) if err_text else "")
    if full:
        ns["_out"] = full

    shown_out, truncated = _truncate(out_text)
    if result_truncated:
        # Keep the untruncated value reachable, same as for stdout.
        ns["_out"] = ns["_"] if isinstance(ns.get("_"), str) else repr(ns.get("_"))
    return {
        "stdout": shown_out,
        "stderr": _truncate(err_text)[0],
        "error": error,
        "delta": _delta(before, ns),
        "result": result_repr,
        "truncated": truncated or result_truncated,
    }


class _SecretGate:
    """What `secrets` is bound to in the interpreter.

    Deliberately not the module: agent-authored code should get a clear refusal
    naming the fix, not a stack trace from somewhere inside the store.
    """

    def __init__(self, store, allowed: set) -> None:
        self._store = store
        self._allowed = allowed

    def __repr__(self) -> str:
        known = ", ".join(self._store.names()) or "none set"
        return f"<secrets: {known} — readable only inside an approved helper>"

    def get(self, name: str):
        return self._store.get(name, registry_names=self._allowed)

    def names(self) -> list:
        return self._store.names()


def _preload() -> dict:
    """Bind approved helpers into the namespace so code can just call them.

    Best effort: a broken helper library must never stop the interpreter from
    starting, or one bad file would take away plain Python too.
    """
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from codeact_mcp import secrets_store
        from codeact_mcp.facade import Helpers
        from codeact_mcp.registry import registry

        reg = registry()
        # Helper modules load as codeact_helper_<name>, which is what the
        # secret guard recognises; agent code never matches.
        allowed = {
            f"codeact_helper_{e.path.stem}"
            for e in reg.entries.values()
            if e.card.meta.requires_secrets
        }
        return {
            **reg.namespace(),
            "helpers": Helpers(reg),
            "secrets": _SecretGate(secrets_store, allowed),
        }
    except Exception:
        return {}


def _install_command_policy() -> None:
    """Layer 0 inside the interpreter: cluster reads yes, cluster writes no.

    The Bash hook keeps governed commands out of the shell; this keeps the
    other half of that promise, which is that the route it points at — an
    approved helper shelling out from in here — stays read-only against
    anything that isn't demonstrably local.

    Two constraints shape it. It has to be lexical: resolving the current
    kubectl context means spawning kubectl, and this hook fires on spawning.
    And it has to allocate nothing surprising, since `open` raises audit events
    too — so the policy is built once, now, and the hook only ever reads it.

    Same disclaimer as everywhere else: a policy layer, not a boundary. Code
    that means to get around an audit hook can.
    """
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from codeact_mcp import commands, config

        policy = commands.Policy.load(config.load())
    except Exception:
        return
    if not policy.blocks_in_codeact:
        return

    def spawn_audit(event: str, args: tuple) -> None:
        if event == "subprocess.Popen":
            argv = args[1]
        elif event in ("os.exec", "os.posix_spawn"):
            argv = args[1]
        else:
            return
        if isinstance(argv, (str, bytes)):
            decision = policy.check_line(os.fsdecode(argv), surface=commands.CODEACT)
        else:
            decision = policy.check_argv(argv, surface=commands.CODEACT)
        if not decision.allowed:
            raise PermissionError(decision.message)

    sys.addaudithook(spawn_audit)


def _install_secret_broker(protocol) -> None:
    """The worker's half of the run_as secret broker (§10).

    Under run_as this process cannot read the 0600 store the human owns, so an
    approved helper's `secrets_store.get` would fail for the one reason that
    is nobody's fault. The fallback asks the parent over the protocol pipe and
    reads the reply off stdin — safe mid-execution, because the parent never
    sends anything unprompted while a block is running.

    The store's caller check still runs in this process before the broker is
    consulted; the parent additionally refuses names no approved helper
    declared, since nothing this process says about its caller can be trusted
    from outside it. Same honest caveat as ever: the plaintext still enters
    this process, so this is delegation for run_as, not the signing-proxy
    strong version.
    """
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from codeact_mcp import secrets_store
    except Exception:
        return

    def request(name: str) -> str | None:
        try:
            protocol.write(json.dumps({"op": "secret", "name": str(name)}) + "\n")
            line = sys.stdin.readline()
            if not line:
                return None
            return json.loads(line).get("value") or None
        except Exception:
            return None

    secrets_store.broker = request


def main() -> None:
    # Move the protocol channel off fd 1 before running anything untrusted.
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), write_through=True)

    ns: dict = {"__name__": "__codeact__", "__builtins__": __builtins__}
    ns.update(_preload())
    # After preloading: an audit hook cannot be removed, and helper imports
    # legitimately spawn nothing, so there is no reason to police startup.
    _install_command_policy()
    _install_secret_broker(protocol)

    # readline rather than iteration: the broker reads its reply from stdin
    # mid-execution, and an iterator's claim on the stream would make that a
    # buffering question instead of a sequencing one.
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        op = msg.get("op")
        if op == "exec":
            payload = execute(ns, msg.get("code", ""))
        elif op == "state":
            payload = {
                "names": [
                    {"name": k, "type": type(v).__name__, "repr": _describe(v)}
                    for k, v in sorted(ns.items())
                    if not _is_noise(k, v)
                ]
            }
        else:
            payload = {"error": f"unknown op: {op}"}

        payload["id"] = msg.get("id")
        protocol.write(json.dumps(payload) + "\n")


if __name__ == "__main__":
    main()
