# codeact-mcp — design

A Claude Code plugin that pushes the agent toward **executable Python as its action
language** (the CodeAct pattern), backed by an MCP server that owns a **session-scoped
interpreter** and a **curated, growing library of helper functions**.

The spine of the design is one distinction:

- **Session state is transient.** Dataframes, fetched pages, parsed ASTs, scratch
  variables — they live in the interpreter, never in the context window, and die with
  the session.
- **Helpers are durable.** When the agent notices it has solved the same sub-problem
  more than once, it proposes a function. A human approves it. It lands in the repo and
  is discoverable forever after.

Everything below serves that split.

---

## 1. Why this beats plain Bash

Claude Code can already run `python -c`. The differences that matter:

| | `Bash(python …)` | `run_python` |
|---|---|---|
| State between calls | none — re-load data every time | persistent namespace |
| Large intermediates | must round-trip through context or files | stay as variables |
| Failure recovery | re-run the whole script | fix one line, keep the loaded state |
| Reusable knowledge | copy-pasted snippets | approved, named, discoverable helpers |

The context economy is the real win. A 200MB dataframe is one variable; the agent
prints `df.head()`, not the frame.

---

## 2. Component map

```
codeact-mcp/                        # this repo = the plugin
├── .claude-plugin/plugin.json      # plugin manifest (only `name` is required)
├── .mcp.json                       # bundled server, launched via ${CLAUDE_PLUGIN_ROOT}
├── skills/
│   ├── codeact/SKILL.md            # model-invoked: teaches the loop + when to propose
│   └── review/SKILL.md             # user-invoked (/codeact:review) approval surface
├── hooks/hooks.json                # session-start catalog injection
├── agents/codeact.md               # optional subagent that works in this style
└── server/codeact_mcp/             # the MCP server (Python, stdio)
    ├── server.py         # MCP tool surface
    ├── interpreter.py    # session manager, timeouts, restarts
    ├── worker.py         # the actual exec sandbox subprocess
    ├── registry.py       # helper storage, load, preload into namespace
    ├── validate.py       # the proposal gate
    └── telemetry.py      # usage counts, repeat-pattern detection
```

Helpers do **not** live in this repo. They accumulate in the *consuming* project:

```
<user project>/.codeact/
├── helpers/<name>.py        # one file per helper — reviewable, diffable, git-tracked
├── proposals/<id>.json      # pending, awaiting human approval
└── telemetry.jsonl          # usage + outcomes (gitignored)
```

Two scopes: **project** (`./.codeact/`, checked in, shared with the team) and **user**
(`${CLAUDE_PLUGIN_DATA}`, personal, survives plugin updates). The catalog merges both;
project wins on a name collision.

---

## 3. MCP tool surface

Deliberately small — every tool costs context in every session.

**Core (4):**

- `run_python(code, timeout_s=30)` → stdout, stderr, traceback, last-expression repr,
  **and a namespace delta**: names created/changed with type and a short repr. The delta
  is what lets the agent keep track of state without printing it.
- `search_helpers(task?, jobs?, domains?)` → a ranked slice of the index, one line per
  helper: `name(sig) — summary [job/domains]`. The front door, and deliberately the same
  ergonomics as discovering MCP tools. See §6 for how the slice is chosen.
- `read_helper(name)` → full source, purpose, examples. Progressive disclosure: the
  index is cheap, bodies are pulled only when needed.
- `propose_helper(name, source, purpose, tags, side_effects, examples)` → runs the
  validation gate, writes a **pending** proposal. Installs nothing.

**Secondary (2):** `session_state()`, `restart_session()`.

**Not agent tools at all:** approve / reject. Those are human surfaces (§5).

Inside the interpreter the same discovery exists as plain Python — `helpers.search(...)`,
`help(fn)` — so code-driven discovery works without leaving the code channel.

---

## 4. The interpreter

- Runs in a **subprocess**, not in the server. A `sys.exit()`, segfault, or runaway loop
  kills the worker, not the MCP server. JSON-lines protocol over pipes; no extra deps.
- Timeout escalates: `SIGINT` first (raises `KeyboardInterrupt`, state survives), then
  `SIGKILL` + restart with an explicit "your state was lost" message.
- cwd pinned to the project root, so file paths behave the way the agent expects.
- Approved helpers are **preloaded** into the namespace at startup, so the agent just
  calls them.
- Output is capped, but the full text is retained as a variable (`_out`) the agent can
  slice — truncation should never lose data, only defer it.
- Resource limits: `RLIMIT_AS`, `RLIMIT_CPU`, no fork bombs.

Sandboxing is tiered, and I'd be honest in the README about which tier is on:
0 = same user, same machine (equivalent to what Bash already grants — the default);
1 = subprocess + rlimits + pinned cwd; 2 = container / `uv` venv with a network toggle.

---

## 5. The accumulation loop

This is the part worth iterating on most.

**Propose.** The skill gives the agent a crisp rule, because otherwise it will spam
proposals: propose only when the same non-trivial pattern has been written **at least
twice**, the interface is stable, it isn't already a helper, and it isn't a one-liner
wrapper around stdlib. The test to give the agent: *would a future session, with no
memory of this task, be better off?*

The server helps: `telemetry.py` mines the execution log for near-duplicate blocks and
volunteers "you've written this three times — consider proposing it."

**Gate.** `propose_helper` rejects before a human ever sees it unless:

- name is a valid identifier, doesn't shadow an existing helper or builtin
- it parses, has type hints on params and return
- it has a docstring with a real *purpose* — what it's for and when to reach for it,
  not a restatement of the signature
- it declares `side_effects` (`none` / `filesystem` / `network` / `process`) and the
  declaration matches a static scan of the source; a mismatch is an outright rejection,
  and anything non-`none` raises the approval's severity
- it declares exactly one `job` and one to three `domains` from the vocabularies in §6,
  and the job is consistent with the declared side effects (a `parse` helper that opens
  a socket is misfiled, and that's worth catching before a human reads it)
- it ships at least one runnable example, executed in a scratch session as a smoke test
- it isn't a near-duplicate of an existing helper (cheap similarity check → "this looks
  like `X`, extend that instead")

**Approve.** Claude Code has a purpose-built mechanism for exactly this, so we don't
hand-roll it: a tool that declares `_meta["anthropic/requiresUserInteraction"]: true` in
its `tools/list` entry prompts the human on **every** call and never offers
"don't ask again" — it cannot be allowlisted away. That flag goes on the install action.
Claude Code also supports **MCP elicitation**, so the server can raise a form dialog
showing the helper's source, purpose and declared side effects, and take the yes/no
there rather than through a bare permission prompt.

`/codeact:review` is the batch surface: a user-invoked skill that renders all pending
proposals as cards with source and diff, for people who'd rather approve five at once
than one at a time mid-task. Nothing installs without a human keystroke either way.

**Promote / demote.** Approval has two scopes: **session** (provisional — usable now,
disappears at session end) and **permanent** (written to `.codeact/helpers/`, git-tracked).
Telemetry drives movement: a provisional helper used successfully N times is offered for
promotion; a permanent helper that keeps raising exceptions gets flagged for revision.

---

## 6. Discovery: categorize by job, retrieve by task

**Settled:** helpers are *not* registered as individual MCP tools. That would burn
context linearly in library size — fighting the point of accumulating many helpers — and
would push them back onto the one-at-a-time JSON channel, when CodeAct's whole claim is
that composing them in code (in a loop, in a conditional, piping one into the next) is
what helps. Discovery gets MCP-like ergonomics; invocation stays in Python.

### The job axis

Every helper declares exactly one **job** — the kind of work it does — drawn from a
closed vocabulary:

| job | what it does | touches the world? |
|---|---|---|
| `acquire` | bring data in from outside the process | yes |
| `parse` | serialized or unstructured → structured | no |
| `transform` | structured → structured: reshape, filter, join, aggregate | no |
| `inspect` | answer a question without changing anything: search, measure, diff, validate | no |
| `present` | format existing data for reading: render, tabulate, summarize | no |
| `generate` | produce a new artifact: code, text, config | no |
| `mutate` | change external state: write, commit, POST | yes |
| `orchestrate` | sequence, retry, or parallelize other helpers | inherits |

Closed on purpose. Free-form tag sets rot within a few dozen entries; adding a new job
category is itself an approval-gated event.

Worth noticing that the job axis and the `side_effects` classification from §5 largely
agree — `acquire` and `mutate` are precisely the categories that touch the world. The
categorization does double duty as safety metadata, and a proposal whose declared job and
declared side effects disagree is a signal the author misunderstood their own function.

A second axis, **domain**, is a curated tag list: `github`, `git`, `fs`, `http`, `data`,
`ast`, `text`, `db`, plus project-specific ones. Each helper takes exactly one job and
one to three domains. One primary job is what keeps the index navigable — a helper that
genuinely spans two jobs is usually two helpers.

Categorization happens **at approval time, not at read time**. The proposal must declare
its job and domains, and the human approving it is validating that classification along
with the code. This is why the taxonomy stays clean: it's curated on write by a human,
never inferred on read.

### Retrieval, three tiers

| tier | cost | what the agent gets |
|---|---|---|
| always in context | ~150 tokens | the **job index** only: categories, one-line definitions, counts — `acquire (12) · parse (8) · transform (15) …` |
| once per task | ~500–800 tokens | signatures + summaries for the slice matching this task |
| per use | full body | `read_helper` on the one or two it will actually call |

Tier 1 means the agent always knows the *shape* of the library without spending a call —
it knows there are twelve ways to acquire things before it knows what any of them are.

The matching in tier 2 splits the work by what each side is good at. **The model does the
semantic step**: reading a task and concluding it's `acquire + transform` over `github` is
exactly what it's good at, and it's already in the loop, so this costs nothing extra and
needs no embedding infrastructure. **The server does exact set filtering and ranking** —
BM25 over name, summary and purpose for the free-text part, then ordering by telemetry
priors: usage count, success rate, recency, and co-occurrence with helpers already called
this session.

So the flow at task start is: agent classifies the task from the job index it already
has, calls `search_helpers(task="…", jobs=[…], domains=[…])`, gets a small ranked slice,
pulls full source for the one it wants, and writes code.

I'd ship lexical matching plus model-chosen categories first and only reach for
embeddings if recall measurably disappoints. Sliced by job and domain, the candidate pool
stays small enough that BM25 is fine well past a hundred helpers.

Two secondary surfaces stay useful: **MCP resources** (`codeact://helpers/<name>`) so a
human can `@`-mention one directly, and **in-interpreter** discovery (`helpers.by_job()`,
`help()`, real docstrings on the preloaded functions) so code-driven lookup works without
leaving the code channel.

### Does the taxonomy actually work?

Categorization schemes are easy to design and hard to validate, so this needs a number
attached to it early. Alongside phase 2: seed the library, write tasks where a specific
helper is the right answer, and measure how often the agent finds and uses it rather than
rewriting the thing from scratch. That hit rate is what tells us whether the job axis is
carrying its weight or whether we're building a filing cabinet nobody opens.

---

## 7. Encouraging Python-first

- **Skill** (`codeact`): triggers on multi-step / data / file / API work. Teaches the
  loop — *check helpers → write Python → read the delta → iterate → propose*. Getting
  the `description` field right is most of the battle; it's what decides whether the
  skill fires on the tasks that deserve it.
- The same skill stays **user-invocable**, so `/codeact:codeact` forces the mode when
  the description didn't trigger on its own.
- **Agent** `codeact` for delegating a whole task in this style.
- A nudge hook (PostToolUse on repeated Read/Bash calls → "this is a `run_python` job")
  is possible but easy to make annoying. I'd ship it off by default, if at all.

---

## 8. Suggested build order

| Phase | Ships | Why here |
|---|---|---|
| 1 | interpreter + `run_python` + skill | useful on its own, before any helper machinery |
| 2 | registry, job/domain taxonomy, `search_helpers`, `read_helper`, preloading, seed helpers, **discovery eval** | proves the retrieval ergonomics against static content, before any of it is load-bearing |
| 3 | `propose_helper`, validation gate, approval surfaces | the actual twist |
| 4 | telemetry, repeat detection, ranking priors, promote/demote | makes accumulation self-sustaining |
| 5 | sandbox tiers, dependency install, user scope | hardening |

Phase 1 is a day. Phases 2–3 are where the design risk is — and phase 2 is worth doing
with hand-written helpers precisely because it lets us measure whether task-driven
discovery works before building the machinery that generates the content.

---

## 9. Open questions

1. **Approval friction.** Both surfaces exist (inline elicitation vs batched
   `/codeact:review`) — which is the default, and does inline approval interrupt the
   agent's flow badly enough that batching should win?
2. **Seed library.** Ship with a starter set (file/AST/HTTP/git utilities) so discovery
   has something to find on day one, or stay empty so everything is earned?
3. **Helper granularity.** One file per helper is maximally reviewable but awkward for
   helpers that want to share private state. Allow modules of related helpers?
4. **Does the agent write helpers, or extract them?** Proposing from *code it already
   ran successfully* is much safer than proposing freshly written code. I lean toward
   requiring the source to have been executed in-session first.
5. **Is the eight-job vocabulary right?** It's a guess, and the honest way to find out is
   to categorize thirty real helpers by hand and see which ones resist classification or
   land in `orchestrate` because nothing else fit.
6. **Should compositions accumulate too?** Individual helpers are the unit now, but the
   recurring thing is often a *sequence* — "for this job, the path is `a()` then `b()`
   then `c()`". Capturing those as recipes is a natural extension of the same flywheel,
   and probably a phase 5+ question rather than something to design in now.
5. **Sandbox default.** Tier 0 matches what Bash already permits and keeps setup at
   zero; tier 2 is defensible but adds Docker/uv as a hard dependency.
