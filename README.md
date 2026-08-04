# codeact-mcp

A Claude Code plugin that gives the agent **executable Python as its action language**,
backed by an MCP server owning a session-scoped interpreter.

Instead of assembling an answer from a long chain of individual tool calls, the agent
writes Python and runs it in a persistent interpreter — with loops, conditionals, error
handling, and composition. Large intermediates stay as variables rather than passing
through the context window.

The longer-term goal is a curated library of approved helper functions that accumulates
over time, discoverable the way MCP tools are. See [DESIGN.md](DESIGN.md) for the full
plan; this repo currently implements **phase 1**.

## Install

```
/plugin marketplace add ssinnott/codeact-mcp
/plugin install codeact@codeact-mcp
```

Then restart, or run `/reload-plugins`.

To try it without installing:

```
claude --plugin-dir /path/to/codeact-mcp
```

**Requirements:** Python 3.9+ on `PATH` as `python3`. There are no third-party
dependencies — the MCP server is pure standard library, so there's no pip install, no
virtualenv, and nothing to build.

## What it adds

| Tool | Purpose |
|---|---|
| `run_python` | Execute Python in the persistent session interpreter |
| `session_state` | List every bound name with its type and a short summary |
| `restart_session` | Discard the interpreter and start fresh |

Plus a `codeact` skill that teaches the working loop and fires on multi-step data, file,
parsing, and API tasks.

### The namespace delta

Every `run_python` result reports which names were created or rebound, so the agent can
track state without printing anything:

```
→ 1000

state: +data (list) 1000 items, +label (str) 'squares', +top (function) top
```

Large containers are summarized by size rather than dumped, imported modules are omitted,
and output over ~4000 characters is truncated with the full text left in `_out` — so
nothing is ever lost, only deferred.

## Behaviour worth knowing

- **State persists** across calls: load data once, then iterate on it.
- **Errors don't reset anything.** A traceback leaves every prior definition intact, so
  you fix one line rather than re-running a whole script.
- **Timeouts interrupt before they kill.** At the deadline the interpreter is sent
  `SIGINT`, which raises `KeyboardInterrupt` and preserves the namespace. Only code that
  ignores the interrupt gets the process killed and restarted, and that says so explicitly.
- **The interpreter is isolated from the server.** `sys.exit()`, a segfault, or a runaway
  loop takes down the worker, not the MCP connection.
- **User output can't corrupt the protocol.** The protocol channel is moved off fd 1
  before any user code runs, so even a subprocess printing to stdout is harmless.

## The guard

A static AST pass classifies what each block touches into capability tiers — tier 0 is the
pure standard library and all syntax, tier 1 is read-only filesystem access, tier 2 is
network, subprocess and dynamic execution, tier 3 is sandbox-escape attribute traversal.

**It currently runs in audit mode: everything is recorded, nothing is blocked.** The tier
boundaries are informed guesses, and the audit log is what will correct them before
enforcement is switched on. It is also a *policy* layer, not a security boundary — Python
cannot be securely sandboxed in-process, and containment is a later phase's job (see
DESIGN.md §9).

## What's recorded

`~/.codeact/corpus.jsonl` logs each executed block with its outcome, duration, project,
and guard findings. Nothing reads it yet; it accumulates now because the pattern miner
that eventually consumes it needs history, and history can't be backfilled.

It stays local, is gitignored, and credential-shaped strings are redacted before writing.

## Development

```
python3 tests/run.py          # 37 tests, stdlib unittest, no dependencies
claude plugin validate .
```

## Licence

MIT
