# codeact-mcp

A Claude Code plugin that gives the agent **executable Python as its action language**,
backed by an MCP server owning a session-scoped interpreter.

Instead of assembling an answer from a long chain of individual tool calls, the agent
writes Python and runs it in a persistent interpreter — with loops, conditionals, error
handling, and composition. Large intermediates stay as variables rather than passing
through the context window.

Alongside it there's a curated library of helper functions, discoverable the way MCP
tools are. See [DESIGN.md](DESIGN.md) for the full plan; this repo currently implements
**phases 1 and 2** — the interpreter, and the helper format with task-driven discovery.
The proposal and approval machinery that lets the library *grow* is phase 3.

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
| `search_helpers` | Find helpers for a task, filtered by job and domain |
| `describe_helper` | The full usage contract for one helper |
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

## The helper library

Helpers are ordinary Python functions carrying a contract. The agent **never sees a
helper's source** — it reads the *card*, the same way it reads an MCP tool's schema
rather than its implementation. That is what makes the documentation load-bearing: with
no source to fall back on, an incomplete card is a missing interface, not a style nit.

```python
from codeact import helper

@helper(job="parse", domains=["data", "fs"], side_effects="filesystem",
        examples=[{"code": "read_jsonl('/tmp/x.jsonl')"}])
def read_jsonl(path: str, *, skip_blank: bool = True) -> list[dict]:
    """Read a JSON Lines file into a list of records, one per line.

    Use when: the file fits in memory and you want every record at once.
    Don't use when: it's larger than memory — iterate and parse line by line.

    Args:
        path: Filesystem path to the file. Must exist and be UTF-8.
        skip_blank: Ignore blank lines. False treats them as corruption.
    Returns:
        list of decoded objects in file order.
    Raises:
        json.JSONDecodeError: a line is not valid JSON; the message carries
            the line number, so use it rather than re-reading the file.
    """
```

Every helper declares exactly one **job** — `acquire`, `parse`, `transform`, `inspect`,
`present`, `generate`, `mutate`, `orchestrate` — plus one to three **domains**. The
vocabulary is closed on purpose; free-form tags rot within a few dozen entries. The two
world-touching jobs are exactly the two that need privileged capabilities, so a declared
job that contradicts declared side effects is a misfiled helper and the gate rejects it.

Discovery runs in three tiers by cost: the job index is cheap enough to always know, a
task-scoped search returns one line per candidate, and the full card is pulled only for
what you'll actually call. The model classifies the task into jobs and domains; the
server does exact filtering plus BM25 and usage priors.

Inside the interpreter the same thing works in code — `helpers.search(...)`,
`helpers.card(name)`, `helpers.jobs()` — since composing in code is the point.

### Examples are contract tests

A helper's examples are executed and their **real output captured into the card**, so
documented behaviour is observed rather than asserted — which rules out a hallucinated
example in a document the agent fully trusts and cannot check. Re-running them later
detects drift, and a helper whose source changed since its examples were verified is
**quarantined**: hidden from search and not preloaded, because documentation known to be
wrong is worse than no helper at all.

```
python3 tools/check.py --capture     # validate, run examples, record output
python3 tools/describe.py <name>     # print a card exactly as the agent sees it
python3 tools/describe.py --index    # the job index and the whole catalog
```

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
python3 tests/run.py          # stdlib unittest, no dependencies
claude plugin validate .
```

## Licence

MIT
