---
name: codeact
description: Use executable Python as the action language for multi-step work — analyzing or transforming data, processing many files, parsing structured formats, calling APIs, or any task where intermediate results feed the next step. Triggers on tasks involving data analysis, CSV/JSON/XML parsing, batch file operations, scraping or summarizing across many inputs, ad-hoc computation, and anything that would otherwise need a long chain of separate tool calls. Also use when a task needs state to persist across steps, when intermediate values are too large to put in context, or when the work involves loops, retries, or conditional branching over results.
---

# CodeAct: think in code, not in tool calls

For anything with more than a couple of steps, write Python and run it with
`run_python` rather than assembling the answer from separate tool calls. Code gives
you loops, conditionals, error handling, and composition — a chain of individual
tool calls gives you none of those.

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

## A note on what's coming

This plugin is being built toward a library of approved, reusable helper functions
that accumulate over time. That machinery isn't here yet. For now, if you notice
yourself writing the same non-trivial block for the second or third time, say so
plainly in your reply — those observations are exactly what the helper library will
be built from.
