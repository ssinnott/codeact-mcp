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

## When a helper is missing or wrong

If you find yourself writing the same non-trivial block for the second or third
time, say so plainly in your reply — that is what the library should absorb, and
those observations are how it grows.

If a helper's behaviour contradicts its card, **report that rather than working
around it**. Quietly writing your own corrected copy hides a defect that would
otherwise get fixed once, for everyone. The card is meant to be trustworthy; a
place where it isn't is worth more as a bug report than as a workaround.
