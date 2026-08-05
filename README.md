# codeact-mcp

A Claude Code plugin that gives the agent **executable Python as its action language**,
backed by an MCP server owning a session-scoped interpreter.

Instead of assembling an answer from a long chain of individual tool calls, the agent
writes Python and runs it in a persistent interpreter — with loops, conditionals, error
handling, and composition. Large intermediates stay as variables rather than passing
through the context window.

Alongside it, a curated library of helper functions that **accumulates**: the agent
proposes, a human approves, and the library is discoverable ever after — the way MCP
tools are. See [DESIGN.md](DESIGN.md) for the reasoning behind every decision.

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
| `propose_helper` | Submit a helper for review. Installs nothing. |
| `helper_source` | Read an implementation — only to revise it |
| `request_capability` | Ask for a privileged capability, session-only |
| `session_state` | List every bound name with its type and a short summary |
| `restart_session` | Discard the interpreter and start fresh |

And one command for you, not the agent — `./codeact`, in this directory:

```
codeact                 what the library and its policies look like right now
codeact review          approve or reject proposals in a browser
codeact mine            what the corpus suggests the library is missing
codeact trace           what a session ran, and what came back
codeact check           validate helpers, capture example output
codeact card <name>     print a card exactly as the agent sees it
codeact policy          which shell commands route through CodeAct
codeact secret          manage secrets helpers may use
codeact sandbox         run the interpreter as a separate OS user
```

Nothing there is reachable from a session, which is the point: approving a helper,
holding a secret, and deciding what may touch a real cluster are decisions the agent
must not be able to make for itself. It's pure standard library, so put it on your
PATH or alias it:

```
alias codeact='python3 /path/to/codeact-mcp/codeact'
```

Plus a `codeact` skill that teaches the working loop and fires on multi-step data, file,
parsing, and API tasks, and a `Bash` hook — off by default — that routes commands you
name into CodeAct instead of the shell.

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
codeact check --capture     # validate, run examples, record output
codeact card <name>         # print a card exactly as the agent sees it
codeact card --index        # the job index and the whole catalog
```

## How the library grows

The agent calls `propose_helper` with a complete module. That **installs nothing** — it
records a pending proposal and runs the whole gate, so your review is spent on judgement
rather than on catching missing type hints. You approve in the browser:

```
codeact review
```

Each candidate shows its card, its source, its declared reach, and a **trial run**. The
trial executes in a scratch directory with no credentials, under `sys.addaudithook`, and
reports *what the code touched* — files written, hosts contacted, processes spawned.
Output alone tells you a candidate is plausible; the effect report is what tells you it
does nothing else, and that second half is the part that actually needs a human.

A revision that **increases** a helper's reach — gaining network access, or a secret — is
flagged on its own rather than left to be spotted in a source diff. A trusted helper
quietly gaining reach looks identical whether the cause is malice or a confused agent.

### Mining

`codeact mine` reads the corpus and produces four queues. New candidates are the
obvious one. The valuable one is **retrieval failures**: inline code that re-implements a
helper you already have, which means the library was fine and *discovery* broke. Those are
two different problems with two different fixes, and they are otherwise indistinguishable.
The other two queues are helpers failing often enough to need revision, and helpers
nothing has called.

Ranking does not use raw frequency. Five uses in one session is one task; five sessions is
a habit; five *projects* is unambiguously a library function. Code that had to be fixed
before it worked ranks higher too, because the helper bakes in a correction that was
expensive to find once.

## The guard

A static AST pass classifies what each block touches into capability tiers — tier 0 is the
pure standard library and all syntax, tier 1 is read-only filesystem access, tier 2 is
network, subprocess and dynamic execution, tier 3 is sandbox-escape attribute traversal.

**It ships in audit mode: everything is recorded, nothing is blocked.** The tier boundaries
are informed guesses and the audit log is what corrects them. Turn it on when you're ready:

```json
// ~/.codeact/config.json
{"guard": "enforce"}
```

Enforcement refuses with a structured message naming two ways forward — request the
capability for this session, or **package the work as a reviewed helper**. The second is
usually right, and it is the point: the friction of the boundary is what channels
privileged work into named, reviewed, reusable units, so every wall the agent hits is an
invitation to propose something.

> **This is a policy layer, not a security boundary.** Python cannot be securely sandboxed
> in-process — attribute traversal, `__globals__`, deserialization — and pretending
> otherwise would be worse than saying so. For a boundary the kernel actually enforces,
> see `run_as` below.

## Keeping commands out of Bash

`kubectl` is two tools wearing one name. Against a local cluster it's an everyday
development tool and blocking it would be pure friction; against a real one it's a
production console with no undo. The difference isn't in the command — it's in which
cluster the config file happens to be pointing at, which no `Bash(kubectl:*)` permission
rule can see.

So a `PreToolUse` hook decides on the **target**, not the verb:

| | Bash | CodeAct |
|---|---|---|
| local cluster (`minikube`, `kind-*`, `docker-desktop`, …) | anything | anything |
| everywhere else | nothing | reads only |

```json
// ~/.codeact/config.json
{"commands": {"mode": "enforce", "rules": ["kubectl", "helm"]}}
```

```
codeact policy enforce                    # off | audit | ask | enforce
codeact policy check 'kubectl get pods'   # decide, without running it
codeact policy block terraform            # Bash-block anything else
codeact policy log                        # what it has seen
```

**It ships off**, like the guard did — `audit` records without blocking, `ask` sends
each one to you, `enforce` refuses. A refusal names the way through:

```
BLOCKED — `kubectl` here targets `prod-eks`, which is not a local cluster.

Bash may target local clusters only (minikube, docker-desktop, kind-*, …). Anything
else goes through CodeAct, where reads are allowed and writes are not:
  1. search_helpers(task="…what you need to read…", job="inspect") — then call it
     from run_python.
  2. propose_helper(...) if nothing fits. Package the read you need; a helper that
     changes state somewhere real will not be approved.

If you meant the local cluster, switch to it first — `kubectl config use-context
<local>` — or name it inline with `--context`.
```

The read-only half is enforced too, not just described: the interpreter refuses to spawn
a mutating command unless the argv names a local context. It decides lexically rather
than resolving the current context, because resolving it would mean spawning `kubectl`
from inside the hook that fires on spawning — so in code the target has to be explicit,
which is a good habit anyway.

Everything unknown counts as production: an unresolvable context, a command bringing its
own `--kubeconfig`, an unrecognized verb, a command line that doesn't parse. A false
positive costs one `--context` flag.

Commands with no notion of a target work too — `codeact policy block terraform` keeps it
out of Bash and leaves CodeAct to helper review, since a helper only runs because someone
read it.

> Same disclaimer as the guard: **policy, not a security boundary.** It understands
> pipelines, `sudo`, `env`, `xargs` and `sh -c`, and fails closed on what it can't parse,
> but a shell is an interpreter and code that means to hide a command from it can. It
> keeps an honest agent out of production; it does not contain a dishonest one.

## Running as a separate user

By default the interpreter runs as you, the same as `Bash` does. Point it at another
account and the containment stops being a matter of policy:

```
sudo useradd --system --no-create-home codeact-runner
codeact sandbox codeact-runner              # checks it, and prints the sudoers line
```

```json
// ~/.codeact/config.json
{"run_as": "codeact-runner"}
```

Verified behaviour under `run_as`: reading a 0600 file you own, listing `~/.ssh`, and
writing outside the project all fail with `PermissionError` from the kernel — not from
an AST walk that a determined `getattr` could sidestep. The session's state still
persists across calls, and a timeout still sends `SIGINT` and preserves the namespace
(sudo relays the signal, which is the part that could easily not have worked).

**Two things to know before turning it on.**

An unprivileged process cannot drop to another user by itself — `subprocess(user=)`
needs `CAP_SETUID` — so this goes through `sudo` and needs a `NOPASSWD` rule scoped to
exactly one interpreter and one target user. `codeact sandbox` prints the line. If the
rule is missing, **the interpreter refuses to start** rather than quietly running with
your full privileges; someone who set `run_as` believes they are isolated.

The runner also cannot write to your project. That is usually what you want from
`run_python` — file edits should go through `Edit`/`Write`, which stay reviewable — but
it will break a helper that legitimately writes files. And because the secret store is
`0600` and owned by you, the runner cannot read it either, which is the right default
and means secret-using helpers need the broker described in DESIGN.md §10.

## Secrets

A helper declares `requires_secrets=["GITHUB_TOKEN"]`, and approving that helper approves
that specific pairing — the token being reachable by one helper says nothing about
anything else. The agent never sees a value.

Three layers, because none alone is enough: a secret is never bound in the namespace; a
`Secret` wrapper redacts through `str`, `repr` and f-strings; and everything leaving the
interpreter is scanned and redacted. That last one is what matters most — an exception
inside an HTTP library will happily print the auth header it was called with, and that
path bypasses the wrapper entirely.

```
codeact secret set GITHUB_TOKEN
```

> Stored under filesystem permissions only (0600). The standard library ships no cipher,
> and rolling one here would read as protection while providing none.

## What's recorded

Two logs, answering two different questions.

**The session transcript** — `~/.codeact/traces/<session>.jsonl`, one file per interpreter
session — is the record of what actually happened: every block of code, its stdout, its
traceback, the last-expression value, the namespace delta, what the guard found, every
restart and every capability you granted, in order, under a header saying which rules were
in force at the time. That last part matters: *what was run* without *what was allowed*
can't tell a block that was permitted from one today's config would refuse.

```
codeact trace                  every session on this machine, newest first
codeact trace last             the most recent one, as a transcript
codeact trace a1b2c3 --code    just the Python, as a script you can re-run
codeact trace --prune 30       drop anything older than 30 days
```

`--code` throws the output away on purpose: for reproducing a session rather than reading
it, the answers are noise and the questions are the artefact. Blocks that were refused or
timed out stay in, commented, so the script can't read as a clean run of something that
never completed.

It's on by default — the reason to want a transcript is almost always a session that has
already gone wrong, and by then it's too late to switch one on. `"trace": false` in
`config.json` turns it off.

**The corpus** — `~/.codeact/corpus.jsonl` — logs each executed block with its outcome,
duration, project, and guard findings, across every session and every project, and
deliberately without output: the pattern miner fingerprints structure, and fingerprints
don't need the answers. Nothing reads it yet; it accumulates now because history can't be
backfilled.

Both stay local, are gitignored, and have credential-shaped strings redacted before
writing — which matters more for the transcript, since it stores output, and a traceback
raised inside an HTTP library prints the header it was called with.

## Development

```
python3 tests/run.py          # stdlib unittest, no dependencies
claude plugin validate .
codeact eval cards            # can a helper be used from its card alone?
codeact eval retrieval        # does task-driven discovery find the right one?
```

The CLI is `codeact` at the repo root — a shim over `server/codeact_mcp/cli/`, one
module per command group. A new human surface is a `register(subparsers)` function and
one line in `MODULES`; it shares the registry, the gate and the config with the MCP
server rather than reimplementing any of them.

## Licence

MIT
