---
name: codeact
description: Use executable Python as the action language for multi-step work — analyzing or transforming data, processing many files, parsing structured formats, calling APIs, or any task where intermediate results feed the next step. Triggers on tasks involving data analysis, CSV/JSON/XML parsing, batch file operations, scraping or summarizing across many inputs, ad-hoc computation, and anything that would otherwise need a long chain of separate tool calls. Also use when a task needs state to persist across steps, when intermediate values are too large to put in context, or when the work involves loops, retries, or conditional branching over results.
---

# CodeAct: think in code, not in tool calls

For anything with more than a couple of steps, write Python and run it with
`run_python` rather than assembling the answer from separate tool calls. Code gives
you loops, conditionals, error handling, and composition — a chain of individual
tool calls gives you none of those.

## The loop

**Check for a helper → write Python → read the delta → iterate.**

Before writing anything non-trivial, call `search_helpers`. The library is where
already-solved problems live, and rewriting one by hand is wasted work that also
tends to be subtly wrong in ways the helper already handles.

## Finding a helper

`search_helpers` takes your task in your own words, plus the categories you think
it falls under. You do the classifying — that part is your job, not the server's.

Every helper has exactly one **job**:

| job | what it does |
|---|---|
| `acquire` | bring data in from outside the process |
| `parse` | turn serialized or unstructured input into structure |
| `transform` | structured to structured: reshape, filter, join, aggregate |
| `inspect` | answer a question about something without changing it |
| `present` | format existing data for reading |
| `generate` | produce a new artifact: code, text, config |
| `mutate` | change external state: write, commit, POST |
| `orchestrate` | sequence, retry, or parallelize other helpers |

and one to three **domains**: `ast`, `data`, `db`, `fs`, `git`, `github`, `http`,
`shell`, `text`, `time`.

Pick one or two of each. Too narrow and you miss the right helper; too broad and
you get noise. "Read a CSV and total a column" is `parse` + `transform` over
`data`, `fs`.

Then call `describe_helper` on anything promising **before using it**. The
signature alone is not enough — the card tells you the exact return shape, what
each parameter means, how it fails, and when *not* to use it. Read it.

Helpers are already imported in the interpreter, so just call them. The same
lookup works in code if you'd rather stay in Python: `helpers.search("...")`,
`helpers.card("name")`, `helpers.jobs()`.

## The interpreter is persistent

`run_python` runs in one long-lived session. Variables, imports, and function
definitions survive between calls. This changes how to work:

- **Load once, then iterate.** Read a file or fetch data into a variable, then
  explore it over several small calls. Don't re-read it each time.
- **Keep big things in variables.** A large dataframe, a parsed tree, or a long
  document should stay in the interpreter. Print a summary — `df.shape`,
  `df.head()`, `len(rows)`, `rows[0]` — never the whole object. Context is the
  scarce resource; interpreter memory is not.
- **Recover in place.** When a call raises, everything defined before it is still
  there. Fix the one line and re-run it, rather than restarting the whole script.

The result of each call lists names that were created or rebound, so you can track
what exists without printing anything.

## Work in small steps

Send a few lines at a time and look at what came back, the way you would use a
REPL. A long script that fails on line 40 wastes everything above it; four short
calls each confirm their own result. Inspect before you assume — check a shape, a
key set, or a first element before writing the code that depends on it.

If a trailing expression has a value, it's reported automatically, so ending a call
with a bare `df.columns` is enough.

## What to reach for

Good candidates for `run_python`:

- Reading, filtering, reshaping, or summarizing data of any size
- Parsing JSON, CSV, XML, HTML, logs, or source code (the `ast` module)
- Operating across many files — searching, rewriting, comparing, counting
- Anything with a loop, a retry, or a branch over intermediate results
- Arithmetic or date handling where being exactly right matters

Prefer a plain `Read` for a single file you just want to look at, and `Bash` for
one-off shell commands. Reach for Python when the work composes.

## Output discipline

Print what you need to see and nothing else. If output is truncated, the full text
is in `_out`, so you can slice it instead of re-running the call.

## Growing the library

When you have written the same non-trivial block more than once, call
`propose_helper`. It installs nothing — a human reviews it — so proposing is cheap
and costs the task nothing.

The test: *would a future session, with no memory of this task, be better off?*
Propose when the interface is stable and it isn't already a helper. Don't propose a
one-liner around stdlib, or something so task-specific nobody would find it again.

Write the docstring as though it is the only thing anyone will ever read about the
function, **because it is** — whoever calls it later sees the card and never the
code. That means: a summary saying what it is *for*, `Use when:` and
`Don't use when:`, every parameter under `Args:`, the *shape* of the result under
`Returns:` (`list of {sha, author, date}`, not `list[dict]`), failure modes under
`Raises:`, and at least one example. Examples are executed and their real output is
recorded into the card, so they must actually run.

## When the guard blocks you

If enforcement is on and your code needs network, a subprocess, or another
privileged capability, you get a refusal naming two routes. Prefer
`propose_helper`: an approved helper carries its capability permanently and every
later session benefits, where `request_capability` lasts only this session and asks
the human every time.

## When Bash refuses a command

Some commands are configured to run through CodeAct rather than the shell — typically
ones that are harmless against a local environment and dangerous against a real one,
like `kubectl`. The refusal says which target it saw and what is allowed instead.

Take the route it names rather than looking for a spelling that gets through. In the
interpreter, reads work against any target, and anything that changes state has to
name a context that is local — the interpreter cannot look up which context is
current, so an unnamed target counts as production. If no helper covers the read you
need, `propose_helper` is the answer; a helper that writes to a real environment is
not going to be approved, so build the read instead.

## When a helper is wrong

If behaviour contradicts the card, **report it rather than working around it**.
`helper_source` exists so you can read the code and propose a fix with
`propose_helper(..., revises=<name>)` — not so you can write your own corrected
copy inline. A silent local workaround hides a defect that would otherwise be fixed
once, for everyone, and the miner will later rediscover your copy as a duplicate of
something that already exists.

Prefer adding an optional argument over a breaking change. A genuinely breaking
change is a new helper with a new name, not a revision.
