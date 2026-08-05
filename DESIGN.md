# codeact-mcp — design

A Claude Code plugin that pushes the agent toward **executable Python as its action
language** (the CodeAct pattern), backed by an MCP server that owns a **session-scoped
interpreter** and a **curated, growing library of helper functions**.

The spine of the design is one distinction:

- **Session state is transient.** Dataframes, fetched pages, parsed ASTs, scratch
  variables — they live in the interpreter, never in the context window, and die with
  the session.
- **Helpers are durable.** When the agent notices it has solved the same sub-problem
  more than once, it proposes a function. A human approves it. It lands in the library
  and is discoverable forever after, in every project.

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
├── skills/codeact/SKILL.md         # teaches the loop, discovery, and when to propose
├── seeds/<name>.py                 # helpers shipped with the plugin, read-only
├── seeds/core/<name>.py            # shared pure code helpers may import (§12, unbuilt)
├── hooks/hooks.json                # PreToolUse(Bash) → the command policy
├── hooks/command_policy.py         # decides, using server/codeact_mcp/commands.py
├── codeact                         # the human CLI — one command, no dependencies
└── server/codeact_mcp/             # the MCP server (Python, stdio)
    ├── server.py         # MCP tool surface
    ├── protocol.py       # hand-rolled JSON-RPC, so there are no dependencies
    ├── interpreter.py    # session manager, timeouts, restarts
    ├── worker.py         # the interpreter subprocess
    ├── registry.py       # helper storage, load, preload into namespace
    ├── cards.py          # parse, validate and render usage contracts
    ├── contract.py       # run examples, capture output, detect drift
    ├── proposals.py      # the gate, and the only path from proposed to callable
    ├── trial.py          # audited trial runs for the review app
    ├── guard.py          # capability tiers, enforcement, refusals
    ├── commands.py       # layer 0: shell commands that belong in CodeAct instead
    ├── secrets_store.py  # Secret wrapper, access control, egress redaction
    ├── corpus.py         # log every executed block + outcome
    ├── search.py         # filter and rank
    ├── taxonomy.py       # the closed job and domain vocabularies
    ├── config.py         # ~/.codeact/config.json
    ├── miner.py          # fingerprint, cluster, rank, the four queues
    └── cli/              # the human surfaces, one module per command group
        ├── overview.py   # `codeact` — library, queue, guard, policy, on one screen
        ├── review.py + review.html   # the approval app: cards, diffs, trial runs
        ├── approvals.py  # pending / show / approve / reject, from a terminal
        ├── library.py    # check (the gate) and card (what the agent sees)
        ├── mining.py     # the four queues, from the corpus
        ├── policy.py     # which commands route through CodeAct
        ├── secret.py     # manage secrets helpers may use
        ├── sandbox.py    # check the run_as boundary and explain it
        └── evals.py      # card sufficiency, retrieval accuracy
```

The CLI lives **inside the package** rather than in a `tools/` directory of scripts.
The human surfaces and the agent surface read the same registry, run the same gate, and
answer to the same config, so they are one program with two front ends — and a human
command that drifted from what the MCP server actually does would be worse than no
command at all. One entry point also means one thing to alias, one `--help` to read, and
one place where a new surface gets added.

Helpers do **not** live in this repo, and — for now — they don't live in the consuming
project either. **One library per machine, in the home directory:**

```
~/.codeact/                  # a local git repo — free history, diff, blame, revert
├── helpers/<name>.py        # code + its card, one file each — tracked
├── core/<name>.py           # shared pure code, no cards, not discoverable (§12, unbuilt)
├── proposals/<id>.json      # pending, awaiting human approval — tracked
├── corpus.jsonl             # every executed block + outcome, all projects — gitignored
├── commands.log             # what the command policy saw and decided — gitignored
└── fingerprints.db          # normalized AST hashes for cross-session mining — gitignored
```

`git init` on that directory is worth doing on day one and costs nothing: it makes helper
history, revision diffs, and rollback into solved problems rather than features (§6). The
corpus stays untracked — it churns constantly and can contain sensitive strings.

Secrets deliberately live **nowhere near** that tree — `~/.codeact-secrets.json`, owner-
readable only (§10).

Single scope, deliberately. Sharing a library across a team is a genuinely harder problem
(approval becomes a supply-chain decision, secrets need a name manifest, and two people can
approve conflicting helpers), and none of it needs solving to find out whether the core
idea works. Punt it.

Two consequences worth planning around, one good and one to watch:

**Good:** the corpus now spans every project on the machine, so the miner (§6) sees far
more history, sooner — and it gains a genuinely strong signal it wouldn't otherwise have.
A pattern recurring across *several different projects* is much more clearly a real
reusable helper than one recurring inside a single project, which is often just one task's
shape repeated.

**To watch:** a helper mined from one project can pollute the catalog for every other one.
Retrieval needs to know which project it's in (§7).

---

## 3. MCP tool surface

Deliberately small — every tool costs context in every session.

**Core (4):**

- `run_python(code, timeout_s=30)` → stdout, stderr, traceback, last-expression repr,
  **and a namespace delta**: names created/changed with type and a short repr. The delta
  is what lets the agent keep track of state without printing it.
- `search_helpers(task?, jobs?, domains?)` → a ranked slice of the index, one line per
  helper: `name(sig) — summary [job/domains]`. The front door, and deliberately the same
  ergonomics as discovering MCP tools. See §7 for how the slice is chosen.
- `describe_helper(name)` → the **card**: the usage contract, *not* the source. See §5.
- `propose_helper(name, source, card, job, domains, side_effects, revises?)` → runs the
  validation gate, writes a **pending** proposal. Installs nothing. `revises` names an
  existing helper and switches the gate into diff mode (§6).

**Secondary (2):** `session_state()`, `restart_session()`.

**Not agent tools at all:** approve / reject. Those are human surfaces (§6).

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

What the agent is *allowed* to call inside that interpreter is the subject of §9.

---

## 5. What a helper is: the card, not the code

**The agent never reads a helper's source.** It reads a **card** — the usage contract.
This is the same relationship it has with an MCP tool: you don't read a tool's
implementation to call it correctly, you read its schema and description. A helper that
needs its source read to be used correctly is a helper with a bad card, and that's a
defect to fix at approval time rather than a gap for the agent to paper over at call time.

Three things fall out of this, and the second is the one that matters:

1. **Context economy.** A helper's body might be eighty lines; its contract is a dozen.
   Across a library of two hundred, that's the difference between a browsable catalog and
   an unusable one.
2. **It forces the documentation to be good.** When source is available, docstrings rot,
   because any ambiguity can be resolved by reading the code. Remove that fallback and the
   card becomes load-bearing — an incomplete contract is now a bug that blocks approval.
   Hiding the source is what gives the gate its teeth.
3. **Real encapsulation.** Internals can be rewritten freely without invalidating anything
   the agent believes, because the agent never believed anything about the internals.

### What a card contains

Roughly a well-written tool schema, and validated like one:

- `name(params) -> return` with full type annotations
- a one-line summary, plus **when to use it and when not to** — the "not" half is what
  tool descriptions usually omit and what most often causes misuse. *"For a single file
  prefer `read_one`; this one pays a directory scan."*
- **per parameter**: meaning, constraints, units, defaults, and what a valid value
  actually looks like
- **return shape**, not just type. `list[dict]` tells the agent nothing; `list of
  {sha, author, date}` tells it everything.
- **failure modes**: which exceptions, under what conditions, and what to do about each
- preconditions — needs auth, needs a git repo, needs the file to exist
- declared capabilities (§9) and cost hints, if it's slow or hits the network
- **one to three worked examples with real inputs and real outputs**

The examples are the interesting part, because they're **executed at approval time and
their actual output is captured into the card**. The documented output is therefore ground
truth rather than the author's guess — which kills hallucinated examples, the most
damaging possible defect in a document the agent trusts and cannot verify.

Those same examples then serve as the helper's **contract tests**. Re-run them on load
(or in CI); if the output no longer matches, the card is stale and the helper is
quarantined from the catalog until a human resolves it. The library self-heals instead of
silently accumulating rot.

The card lives *in* the helper file as structured metadata plus a rich docstring, and the
registry compiles the catalog from it. One source of truth, so the card and the code can't
drift apart.

### The one escape hatch

Source access exists, but through a separate narrow door — `helper_source(name)` — and
only for the revision workflow (§6): the agent proposing a change to an existing helper
does legitimately need to see it. It is not part of the discovery path, and reading source
is expected to be followed by a `propose_helper(..., revises=…)` rather than by the agent
quietly writing its own corrected copy inline.

Debugging deliberately does *not* qualify. If a helper fails in a way its card didn't
predict, the correct outcome is a flagged helper and a human fix, not an agent reverse
engineering the body and routing around the damage. The miner's revision queue (§6)
already watches failure rates; an unexplained failure should feed that, not be silently
absorbed by an agent working around it.

---

## 6. The accumulation loop

Candidates arrive on **two tracks**, and the second is the one that carries the weight.

### Track 1 — in-session, opportunistic

The agent proposes when it's obvious and cheap. The skill gives it a crisp rule, because
otherwise it will spam proposals: propose only when the same non-trivial pattern has been
written at least twice, the interface is stable, it isn't already a helper, and it isn't a
one-liner around stdlib. The test: *would a future session, with no memory of this task,
be better off?*

This track is real but unreliable, and the design shouldn't lean on it. Mid-task, the
agent is task-focused and correctly reluctant to stop and do library maintenance — so it
will do a lot inline. More fundamentally, the strongest evidence for a helper is
*repetition across sessions*, which by construction is invisible from inside any one of
them.

### Track 2 — offline mining

So the systematic path is a background job over the corpus of everything ever executed.
Every `run_python` call already passes through the server, so it logs the block, its
outcome, duration, which helpers it called, which capabilities the guard flagged (§9),
and enough task context to say what it was for.

The pipeline: **normalize → cluster → rank → synthesize → queue for review.**

*Normalize and cluster.* Canonicalize the AST — local names to positional placeholders,
literals to type tokens — then fingerprint subtrees. That catches same-shape-different-
names, which is the common case and what plain text hashing misses. A second pass over
call-sequence n-grams (`open → json.load → filter → sort`) catches idioms that recur
without being structurally similar. Embedding-based clustering is the expensive fallback
if those two underdeliver; I'd not start there.

*Rank.* Frequency alone is a bad signal. What actually predicts a good helper:

- **spread across sessions, and across projects** — five uses in one session is one task;
  five sessions is a habit; five *projects* is unambiguously a library function, and with
  a single home-dir corpus (§2) that signal is available for free
- **failure cost** — code that errored and needed fixing before it worked is high-value to
  encapsulate, because the helper bakes in the correction that was expensive to find
- **shape stability** — if the pattern is still churning between sessions it's premature
  to freeze; if it's converged, it's ready
- **capability hits** — blocks the guard flagged are already privileged work looking for
  a reviewed home (§9)
- **near-miss against the existing library** — if a helper almost covers it, this is a
  revision, not a new helper

*Synthesize.* Clustering finds candidates; turning a cluster into a well-parameterized
function with a complete card (§5) needs a model. That's an LLM pass over each surviving
cluster, out of band, producing a proposal that then enters the same gate as track 1.

### The miner produces four queues, not one

New-helper candidates are only the obvious output. Three more matter as much:

- **Revision candidates** — helpers that fail often, or that near-misses suggest should be
  generalized
- **Removal candidates** — helpers nothing has called in months, so the catalog doesn't
  bloat with things that only cost tokens to list
- **Retrieval failures** — inline code that matches the fingerprint of a helper that
  *already exists*. This is the most valuable output of the whole miner: it means the
  library was fine and **discovery** failed. That's a continuous, free eval of §7's
  retrieval design running against real usage, and it distinguishes "we need another
  helper" from "the agent couldn't find the one we have" — two problems with completely
  different fixes that are otherwise indistinguishable.

### When it runs

Out of the hot path, always. Mining during a session would compete with the task for
exactly the attention the task needs. Cheap incremental fingerprinting can run at
`SessionEnd`; the expensive clustering and synthesis run as a periodic batch, since
cross-session spread is the signal and that needs more than one session to exist.

Results surface as a digest in `/codeact:review` — *"4 candidates from the last 12
sessions, 2 retrieval failures"* — reviewed in batch at a moment the human chose.

**Log from day one, mine later.** Same shape as the guard's audit mode: the corpus has to
start accumulating in phase 1 even though nothing consumes it until much later, because a
miner switched on against an empty history has nothing to find.

One caveat to design in rather than bolt on: the corpus is executed code, which can
contain secrets pulled from the environment or from files. It stays local and gitignored,
long string literals are redacted before fingerprinting (fingerprints don't need them),
and the synthesis pass sending code to a model should be an explicit, documented choice.

### Shared by both tracks

**Gate.** `propose_helper` rejects before a human ever sees it unless:

- name is a valid identifier, doesn't shadow an existing helper or builtin
- it parses, has type hints on params and return
- **the card is complete** (§5): every parameter documented, return *shape* described,
  failure modes enumerated, and a when-not-to-use clause present. Since the agent will
  never see the source, an incomplete card is not a style nit — it's the whole interface
  missing, and this is the strictest check in the gate
- it declares `side_effects` (`none` / `filesystem` / `network` / `process`) and the
  declaration matches a static scan of the source; a mismatch is an outright rejection,
  and anything non-`none` raises the approval's severity
- it declares exactly one `job` and one to three `domains` from the vocabularies in §7,
  and the job is consistent with the declared side effects (a `parse` helper that opens
  a socket is misfiled, and that's worth catching before a human reads it)
- its examples run in a scratch session, and their **real output is captured back into
  the card** — documented behaviour is observed, never asserted
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

**Promote / demote.** Approval has two lifetimes: **session** (provisional — usable now,
disappears at session end) and **permanent** (written to `~/.codeact/helpers/`).
Telemetry drives movement: a provisional helper used successfully N times is offered for
promotion; a permanent helper that keeps raising exceptions gets flagged for revision.

### Revising an existing helper

A revision differs from a new proposal in exactly one way that matters: **there is already
a published contract**. So the question isn't "is this code any good" — the gate answers
that identically for both — it's *what does this change do to the card?* Classify by that,
and the workflow falls out.

**Prefer optional arguments.** Python makes compatible extension the path of least
resistance — a new keyword argument with a default keeps every existing call valid — and
the gate should actively push authors that way, because a revision that could have been
additive but wasn't costs a name and a retirement for nothing. Most "breaking" changes
aren't, if you reach for a default value first.

**When it genuinely does break, it's not a revision. It's a new helper.** If the signature
changes incompatibly, the return shape changes, or the semantics change enough that the old
examples no longer hold, the answer is a new name, and the old helper goes to the removal
queue if it's now redundant.

This is worth stating firmly because it deletes an entire subsystem. Versioning exists to
manage the pain of migrating coordinated callers — and there are no coordinated callers
here. A helper's callers are future agent sessions that read the card fresh every time;
nothing is pinned to an old signature, nothing needs a deprecation window, nothing needs a
migration guide. **No version numbers on helpers.** Rename and retire instead.

That leaves three real classes, each with a different amount of ceremony:

| class | what changed | path |
|---|---|---|
| **body-only** | implementation; card byte-identical, examples still produce the captured outputs | full gate, standard review. From the agent's side *nothing happened* — it never saw the body (§5) |
| **compatible extension** | new optional parameter, extra return field, newly documented failure mode; existing examples still pass unchanged | full gate, review with a card diff |
| **card-only** | wording, a clarified constraint, a better when-not-to-use | fast path: diff review, nothing executes but the examples, no capability re-grant |
| **any capability or secret increase** | — | escalated, regardless of the above |

That last row is the one to get right. A revision that *adds* `network`, or adds a
declared secret, is the single most dangerous edit in the system — a benign, trusted,
already-approved helper quietly gaining reach is precisely what a supply-chain attack
looks like, and it looks the same whether the cause is malice or a confused agent. So
privilege increases get the same full interaction as a brand-new privileged helper, and
the app shows the capability delta prominently rather than burying it in a source diff.
Privilege *decreases* are safe and can fast-path.

**The gate always re-runs, in full.** It's cheap, a revision is new code by definition, and
deciding which checks to skip is more complexity than just running them all. What changes
for a revision is that the gate reports a *diff* — card, source, capabilities, secrets —
rather than a bare pass/fail.

**The old version stays live while a revision is pending.** A proposal is a proposal; it
shouldn't change the world just by existing, and taking a working helper away because
someone suggested an improvement is a bad trade. The one exception is a helper already
quarantined for failing its contract tests (§5), which is the case the open question
worried about — and it turns out to be the *healthy* path rather than a conflict: the
failure is what created the revision pressure, and the revision is the fix. The app should
link the two and float that pair to the top of the queue, since it's the only queue item
where something is currently broken.

Quarantine means **removed from search and not preloaded**, not flagged-but-present. The
agent trusts cards absolutely and cannot verify them (§5), so a helper with known-wrong
documentation is worse than no helper at all. `describe_helper` on it still answers, with
the quarantine reason, so anything referencing it learns why instead of getting a bare
not-found.

**Who writes it.** Three authors, one flow. The agent proposes a revision when it hits a
limitation mid-task — this is the one situation where `helper_source` is legitimate (§5) —
and passes `revises=<name>`. The miner proposes revisions from its revision queue. The
human edits directly in the app. All three land in the same gate and the same review.

One rule for the agent, stated in the skill: on hitting a broken or insufficient helper,
the options are *propose a revision* or *report it*. Inlining a fixed copy and moving on is
the failure mode to design against — it silently forks the library into the corpus, where
the miner will eventually rediscover it as a near-duplicate cluster and propose re-adding
what already exists.

**Unblocking mid-task** is what the existing session-scope approval is for: approve a
revision provisionally, the agent continues with the fix, permanent promotion happens later
in the app at a moment of the human's choosing. No one has to do a careful review under
time pressure to unblock a task.

**Rollback** needs no design: `~/.codeact/` is a local git repo (§2), so a bad revision is
`git revert`, and history, diff, and blame come free. The miner notices the regression on
its own — a spike in failure rate is already a revision-queue trigger.

---

## 7. Discovery: categorize by job, retrieve by task

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

Worth noticing that the job axis and the `side_effects` classification from §6 largely
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
| per use | the **card** (§5) | `describe_helper` on the one or two it will actually call — contract, never source |

Tier 1 means the agent always knows the *shape* of the library without spending a call —
it knows there are twelve ways to acquire things before it knows what any of them are.

The matching in tier 2 splits the work by what each side is good at. **The model does the
semantic step**: reading a task and concluding it's `acquire + transform` over `github` is
exactly what it's good at, and it's already in the loop, so this costs nothing extra and
needs no embedding infrastructure. **The server does exact set filtering and ranking** —
BM25 over name, summary and purpose for the free-text part, then ordering by telemetry
priors: usage count, success rate, recency, and co-occurrence with helpers already called
this session.

Because the library is machine-wide (§2), **project affinity** joins that ranking: a
helper that has only ever been used in one project ranks low everywhere else, while one
used across several ranks as genuinely general. This is what stops a single-purpose helper
mined out of one codebase from cluttering the catalog for every other one. It's a ranking
signal rather than a filter, so a helper can still be found outside its home project when
it's genuinely the right answer — it just doesn't lead with it.

So the flow at task start is: agent classifies the task from the job index it already
has, calls `search_helpers(task="…", jobs=[…], domains=[…])`, gets a small ranked slice,
pulls the card for the one it wants, and writes code against the contract.

I'd ship lexical matching plus model-chosen categories first and only reach for
embeddings if recall measurably disappoints. Sliced by job and domain, the candidate pool
stays small enough that BM25 is fine well past a hundred helpers.

Two secondary surfaces stay useful: **MCP resources** (`codeact://helpers/<name>`) so a
human can `@`-mention one directly, and **in-interpreter** discovery (`helpers.by_job()`,
`help()`, real docstrings on the preloaded functions) so code-driven lookup works without
leaving the code channel.

### What the phase-2 evals actually measured

Both evals ran against the twelve seed helpers. Recording the numbers here because they
are the evidence for design decisions made above, not decoration:

| metric | result |
|---|---|
| retrieval hit@1 | 12/12 after making domain a ranking signal (11/12 before) |
| card sufficiency, code runs | 10/10 of well-formed submissions, from the card alone |
| reader judged the card enough | 11/12 |
| gaps readers reported anyway | 73 across 12 cards |

The last row is the one that matters. Code that runs is a weak signal — a reader can
guess right. The 73 specific complaints, filed by readers who *did* produce working code,
are what showed the cards were thinner than they looked, and they found a defect class the
gate cannot see: a bare `str` satisfies `Sequence[str]` and `Collection[str]`, so
`ignore="owner"` type-checks and then silently iterates characters. Three separate helpers
had it. **Type-correct and silently wrong is invisible to both the validator and the
contract tests**, which is a real limit on how much the gate can promise.

### Does any of this actually work?

Categorization schemes are easy to design and hard to validate, so this needs numbers
attached early. Two evals, both cheap, both alongside phase 2.

**Retrieval hit rate.** Seed the library, write tasks where a specific helper is the right
answer, and measure how often the agent finds and uses it rather than rewriting the thing
from scratch. That tells us whether the job axis is carrying its weight or whether we're
building a filing cabinet nobody opens. Once the miner exists (§6) this stops being a
fixed test suite and becomes continuous: its retrieval-failure queue *is* this metric,
measured against real usage rather than seeded tasks.

**Card sufficiency.** Because the card is now the entire interface, it can be tested
directly: give a fresh agent the card *alone* — no source, no context from the session
that produced it — and have it use the helper. If it can't, the card is inadequate and the
helper goes back for revision. This is a rare case where the safety property and the eval
are the same mechanism, and it gives approval reviewers an objective standard to apply
instead of a vibe.

---

## 8. Encouraging Python-first

- **Skill** (`codeact`): triggers on multi-step / data / file / API work. Teaches the
  loop — *check helpers → write Python → read the delta → iterate → propose*. Getting
  the `description` field right is most of the battle; it's what decides whether the
  skill fires on the tasks that deserve it.
- The same skill stays **user-invocable**, so `/codeact:codeact` forces the mode when
  the description didn't trigger on its own.
- **Agent** `codeact` for delegating a whole task in this style.
- A nudge hook (PostToolUse on repeated Read/Bash calls → "this is a `run_python` job")
  is possible but easy to make annoying. I'd ship it off by default, if at all.
- A **blocking** hook is a different animal, and it shipped: `PreToolUse` on `Bash`
  refuses configured commands and names the CodeAct route instead (§9, layer 0). It is
  not an encouragement mechanism — nobody should route work through the library because
  a nag suggested it — but where a command is genuinely too sharp for a shell, the
  refusal is the honest place to say so.

---

## 9. The interpreter guard

**Decided:** the agent *authors* helpers rather than only extracting previously-executed
code, and the guard is what makes that safe. Authoring is where the value is — the agent
can write the function it wishes existed, generalize beyond the one case it just solved,
and give it a real interface. Sign-off is what contains the risk.

### Two layers, and not confusing them

This is the one part of the design where a mistake is expensive, so it's worth being
blunt: **you cannot securely sandbox Python in-process.** The language's dynamic nature
means restriction is a losing game — attribute traversal (`().__class__.__bases__`),
`__globals__` on any function object, deserialization, dynamic import — and new escape
routes surface regularly. RestrictedPython, the most mature tool in this space, says so
itself in its own README: it "is not a sandbox system or a secured environment," it
helps *define a trusted environment*. That framing is exactly right and worth stealing.

So the guard is two layers with two different jobs:

**Layer 1 — the policy layer (AST analysis).** Static walk of every block before it
executes. This is where "non-approved functions don't run" lives. Its job is *intent and
review*: catch unreviewed capability use, explain it well, and route it into the approval
flow. It is defeatable by sufficiently dynamic code, and that is acceptable, because it
is not the security boundary.

**Layer 2 — the containment layer (OS-level).** The actual boundary. On Linux,
**Landlock + seccomp-bpf** is the sweet spot for a local dev tool: roughly 6ms startup,
near-native performance on the hot path, no Docker dependency, and it constrains
filesystem and syscall access for real. Containers or gVisor if the server is ever hosted
for someone else; microVMs if it's multi-tenant. WASM/Pyodide is a poor fit here despite
being a genuine boundary — an agent's Python routinely wants native extensions and
persistent state, which is exactly what that model gives up.

Layer 1 decides what *should* run and asks a human when unsure. Layer 2 decides what
*can* damage anything. Keep them separate in the code and in the docs, so nobody mistakes
a policy check for a security guarantee.

### Capability tiers

The tiering answers the "subset of the stdlib and basic syntax" problem directly.

**Tier 0 — always available, no approval.** All language syntax: control flow,
comprehensions, functions, classes, exceptions, f-strings. The agent writes whatever it
wants here. Safe builtins (`len`, `sorted`, `enumerate`, `sum`, `isinstance`, `print`,
the constructors, …) and the pure stdlib: `json`, `re`, `math`, `statistics`,
`itertools`, `functools`, `collections`, `dataclasses`, `datetime`, `decimal`, `textwrap`,
`difflib`, `hashlib`, `base64`, `uuid`, `enum`, `typing`, `csv`, `io`, `copy`, `heapq`,
`bisect`, `ast`, `pprint`, `operator`. Plus every approved helper. This tier is
deliberately generous — pure computation on data already in the namespace can't hurt
anyone, and a stingy tier 0 produces an agent that flails against the guard instead of
working.

**Tier 1 — allowed, logged, no prompt.** Reading files under the project root. The
argument for not gating this: Claude Code already grants `Read` and `Bash`, so blocking
file reads in Python buys no security and costs a lot of friction.

**Tier 2 — requires approval.** Network (`urllib`, `requests`, `httpx`, `socket`),
process spawning (`subprocess`, `os.system`), writes outside the project root, `ctypes`,
`pickle.load`, and dynamic execution (`eval`, `exec`, `compile`, `importlib`).

**Tier 3 — refused outright**, absent an explicit config override: dunder traversal used
for escape (`__class__`/`__bases__`/`__subclasses__`/`__globals__`/`__mro__`), and any
write to `~/.codeact/` itself. The agent must not be able to edit the guard, the approved
helpers, or the proposal queue — that's the one privilege escalation that breaks the
entire model.

Note that tier 2 is *exactly* the `side_effects` vocabulary from §6, which is *exactly*
the `acquire`/`mutate` split from §7. Side-effect declaration, job classification, and
capability grant are three views of the same metadata. That's not a coincidence worth
engineering around — it's a sign the model is coherent, and the implementation should use
one enum for all three.

### The escalation path is the flywheel

When code hits a blocked capability, the guard must not just throw. It returns a
structured refusal naming the capability, the line, and the two ways forward:

```
BLOCKED  requests.get  →  capability `network` (tier 2), line 4
  This session has no network grant.
  → request_capability("network", reason=…, scope=session|once)   human prompt
  → propose_helper(...)  package it as a reviewed helper, approved once, reusable forever
```

That second option is the interesting one, because it means **the guard is the primary
driver of helper accumulation**. Rather than hoping the agent notices it has repeated
itself, the friction of the capability boundary continuously channels privileged work
into named, reviewed, reusable units. Every wall the agent hits is an invitation to
propose something.

It also resolves the authoring tension cleanly: the agent has total freedom exactly where
freedom is safe (tier 0, pure computation) and needs a human exactly where it isn't.

### Helpers run elevated

The mechanic that makes this work: **an approved helper's body runs with the capabilities
it declared at approval time**, even though agent-written code cannot use those
capabilities directly. `fetch_pr_diff()` may use `requests` internally because a human
read that code and approved it. Helpers are loaded from trusted files and are not subject
to the layer-1 walk — they were *reviewed* instead of *analyzed*.

So the helper library is a growing set of reviewed capability capsules, and the approval
gate is the only bridge between the guarded and unguarded worlds.

### Rollout: audit mode first

The main risk here is false positives — a guard that blocks too much produces an agent
that fights it. So ship the guard in **audit mode**: it walks, logs what it *would* have
blocked, and blocks nothing. Run it against real sessions, tune tier 0 against what
legitimate work actually touches, and only then turn enforcement on. The audit log is
also the honest input to the tier boundaries above, which are currently my guesses.

One UX detail that matters more than it sounds: the guard should report **all** violations
in a block at once, not the first. Otherwise a five-violation snippet costs five
round trips to fix.

### Layer 0: commands that shouldn't be in Bash at all

The two layers above both start from "the agent is already inside the interpreter". There
is an earlier question they can't answer, because `Bash` sits outside all of this: which
commands should reach a shell in the first place?

The motivating case is `kubectl`, and it is not really about kubectl. A single binary is
both an everyday development tool and a production console, and which one it is depends
entirely on **what it is pointed at** — a fact that lives in a config file rather than in
the command line being run. `Bash(kubectl:*)` in a permission rule cannot see that
difference. Neither can a human skimming a tool call.

So the policy is stated in terms of the target rather than the verb:

| | Bash | CodeAct |
|---|---|---|
| local cluster | anything | anything |
| everywhere else | nothing | reads only |

Why route the remainder into CodeAct rather than just denying it? Because denial with no
route produces an agent that reformulates until something gets through, and because the
work is usually legitimate — reading from staging to answer a question is fine, and the
thing to prevent is *changing* it. Sending that through the library means the read arrives
as a named helper somebody approved, which is the same flywheel as §9's escalation path:
every wall is an invitation to propose something.

**Two enforcement points, because they know different things.** A `PreToolUse` hook on
`Bash` runs as its own process and can afford to ask `kubectl config current-context` what
the command would actually hit, so it resolves the implicit target and refuses anything
non-local. Inside the interpreter, an audit hook on process spawning cannot do that —
resolving the context means spawning kubectl, and the hook fires on spawning — so it
decides lexically: read-only verbs pass against any target, and a mutating verb passes
only when the argv itself names a local context. That asymmetry is a real constraint
honestly handled rather than a gap, and it produces a habit worth having: state-changing
code says out loud what it is aimed at.

**Everything unknown is treated as production.** An unresolvable context, a command that
brings its own `--kubeconfig`, a verb not on the read-only list, a command line that
doesn't parse — all refused. The cost of a false positive is one `--context` flag; the
cost of a false negative is a deleted namespace.

The same disclaimers as the rest of §9 apply, doubled. This parses shell text, and a shell
is an interpreter: pipelines, `sudo`, `env`, `xargs` and `sh -c` are understood, and
something determined to hide a command from it can. It exists to keep an honest agent out
of production, not to contain a dishonest one. And the read-only verb lists are the same
kind of informed guess the capability tiers were, which is why this ships **off**, with
`audit` and `ask` sitting between off and `enforce`, and a log of everything it saw.

Ruleset lives in `~/.codeact/config.json`; commands with no notion of a target (`terraform`,
say) can be listed too, and get the simpler treatment — blocked in Bash, left to helper
review in CodeAct, since a helper only exists because a human read it.

---

## 10. Secrets

The requirement — the agent must never see a secret, but an approved helper must be able
to use one — is exactly the capability delegation from §9, applied to data instead of
syscalls. Helpers already run elevated because a human read them. Secrets extend that:
**approving a helper is also approving its access to the specific secrets it declared.**

### The binding

A helper declares `requires_secrets: [GITHUB_TOKEN]` in its card. That declaration is part
of what the human approves, so authorization is a `(secret, helper)` pair, not a global
grant. `GITHUB_TOKEN` being available to `fetch_pr_diff` says nothing about whether it's
available to anything else. That's the "only usable by the calling function" property, and
it falls out of the gate we already have rather than needing new machinery.

The card documents the requirement as a precondition, so the agent knows a helper needs
auth without ever learning what the credential is.

### Three layers, because one isn't enough

**1. Never in the namespace.** The secret is not a variable. Agent-authored code has no
name bound to it, and `session_state()` cannot surface it. `secrets.get()` exists but is
callable only from trusted helper module code — checked by walking the calling frame back
to its defining module and testing it against the approved-helper registry. Agent code
executed via `exec` has a distinguishable frame identity, so this is a real check, with
the same honest caveat as the AST guard: it stops accidental and casual access, and it is
not the security boundary.

**2. Opaque wrappers.** What `secrets.get()` returns is a `Secret` object whose `__str__`,
`__repr__`, and `__format__` all yield `<secret:GITHUB_TOKEN>`. The plaintext resolves
only at a trusted call boundary. This is aimed squarely at the dominant real-world failure
mode, which is not exfiltration but *accident* — a credential landing in a log line, an
f-string, or a traceback.

**3. Egress redaction.** Everything leaving the interpreter for the model — stdout,
stderr, tracebacks, the namespace delta, the corpus log — is scanned against the known
secret values and redacted before the model sees it. A plain substring scan is enough and
costs nothing. **Tracebacks are the critical case**: an exception raised inside an HTTP
library will happily print the auth header it was called with, and that path bypasses
every wrapper in layer 2. This backstop is what makes the other two layers survivable.

### The strong version

The architecturally correct answer is that **the secret never enters the interpreter
process at all**. Helpers don't receive credentials; they ask a broker in the server
process to perform the privileged operation, and the broker attaches auth itself. For HTTP
this is clean — a signing proxy that adds headers the sandbox never sees — and it composes
with the layer-2 containment from §9, since the worker then has no credential to leak
regardless of what runs inside it.

I'd ship layers 1–3 first because they're cheap and cover accident, and treat the broker
as the hardening step for anything genuinely sensitive. Worth being explicit in the README
about which mode is on, the same as with the sandbox tiers.

### Storage and audit

Encrypted at rest with an AEAD, keyed from the OS keyring where one exists — encryption,
not obfuscation. Each secret gets a random opaque handle so a name can't be guessed.
Stored outside `~/.codeact/` and never in any repo. Every access is logged with
the helper that made it and when, which feeds the same review surfaces as everything else:
a helper that starts reading a secret it never used before is exactly the kind of drift a
human should see.

---

## 11. The review app

The queues from §6 need somewhere to be processed, and a terminal prompt is the wrong
shape for it — reviewing a candidate means reading a card, reading source, seeing the
evidence that produced it, and watching it actually run. That's a UI.

A local web app, launched by `codeact review`, which starts an HTTP server bound to
127.0.0.1 with a random token in the URL and opens a browser. Deliberately a **separate
process from the MCP server**: reviewing is the human's activity, it shouldn't require an
active Claude session, and the MCP server is stdio-bound and per-session anyway. Both read
and write the same `~/.codeact/` directory.

Keep it dependency-light and buildless — stdlib HTTP server, one HTML file, fetch calls.
The whole point is that it starts instantly and has no toolchain.

### What a candidate looks like on screen

- the **card** as the agent will see it, rendered exactly as delivered
- the **source**, which the human reads and the agent never does (§5)
- the **evidence**: the inline occurrences the miner clustered, with session and date, so
  "this pattern appeared in six sessions over three weeks" is visible rather than asserted
- the **ranking signals** that surfaced it, and the gate's validation results
- declared **capabilities and secrets**, which is what's actually being granted

### Running a candidate is the dangerous part

The Run button is the single biggest hole in this design if built naively, and it's worth
naming that plainly: a pending candidate is **unreviewed code**, and the trial run is the
moment a human is most inclined to execute it without thinking, precisely because they're
trying to decide whether to trust it. So:

- Trial runs execute under the **strictest containment tier** (§9 layer 2), not the tier a
  normal session uses.
- They run against a **scratch copy** of the project — temp dir or git worktree — so
  writes can't touch anything real.
- Declared capabilities are granted **provisionally and individually**, each OK'd by the
  reviewer before the run.
- **Real secrets are never supplied by default.** Placeholders unless the reviewer
  explicitly opts in, per run. Otherwise "let me just test it" becomes the exfiltration
  path, and it's the most plausible way this whole design gets someone's token stolen.

### Show side effects, not just output

The output alone doesn't tell you a candidate is safe — it tells you it's *plausible*. So
a trial run produces a **side-effect report** next to the return value: files read and
written, hosts contacted, processes spawned, secrets touched. That's what lets a reviewer
conclude both "it works" and "it does nothing else," and the second half is the one that
actually needs a human.

### The trial run feeds the card

One nice consequence: the card's examples are the default test inputs, one click runs them
all, and their **real captured output becomes the card's documented output** on approval.
The mechanism that convinces the human it works is the same mechanism that makes the
documentation ground truth (§5). The reviewer's confidence and the agent's contract come
from a single execution.

---

## 12. Shared code, and composition

Two asks, one new thing. "Helpers should share a core library" and "helpers should
compose" are the same edit to the model: **a helper may now depend on something other
than the standard library.** Everything below follows from what that edge is allowed to
point at, and from what has to happen when the thing at the far end changes.

Today there is no such edge, and not by omission — it's enforced by accident. Helper files
load by path under a generated module name (`codeact_helper_<stem>`) with neither helper
directory on `sys.path`, so a sibling import raises `ModuleNotFoundError` at load time,
the registry files the file under `errors`, and the helper is simply absent from search.
Measured, not assumed: put the directory on the path and the same file loads fine, and the
registry's `__module__` check already declines to re-register an imported helper as a
second entry. The plumbing half is nearly there. What's missing is everything that decides
whether an edge is *safe*, and that failure mode — silently absent, with the reason
printed only by the CLI — is the first thing to fix, because it's about to get common.

### Two layers, because they answer different questions

The distinguishing question between shared core code and a helper isn't "is it reusable" —
both are — but **who reads it**. A helper's audience is the agent, which never sees the
source and works entirely from the card (§5). A core module's audience is the *author* of
a helper, human or agent, who does see the source and therefore needs no card.

That gives a rule sharp enough to settle every "should this be core or a helper" argument:
**if the agent should be able to find it and call it, it's a helper; if only helper code
should call it, it's core.** Core is never preloaded into the namespace, never indexed,
never returned by search, and has no card. Give a core module a card and you have written
a helper with extra steps.

| layer | lives in | may import | reviewed as | the agent sees |
|---|---|---|---|---|
| session code | `run_python` | everything below | not at all | it wrote it |
| **helpers** | `~/.codeact/helpers/<name>.py`, `seeds/` | stdlib · core · helpers | card + gate + trial run | the card, via search |
| **core** | `~/.codeact/core/<name>.py`, `seeds/core/` | stdlib · core | source diff + its dependents' examples | nothing |
| stdlib | — | — | — | it already knows it |

Imports resolve through one name, so an edge is greppable and unambiguous: `from
codeact.core import records` and `from codeact.helpers import group_by`. That's the
existing `server/codeact.py` shim (which exists precisely so a helper file stays
importable standalone) growing two submodules that resolve against the user's directory
first and the shipped one second — the same shadowing rule the registry already uses.

### Core is pure, by rule

Not by convention. The gate can check a declared job against declared side effects (§7)
only because a helper's body is the sole place its reach comes from. The moment that body
can call into shared code, the check is worth exactly as much as whatever the shared code
is allowed to do. Two ways to keep it honest: propagate effects out of core the way we're
about to propagate them along helper edges, or forbid core from having any.

Forbidding is better, and it costs nothing, because **shared code that touches the world
is already a helper** — that's what `retry_call` is. Core is where a percentile
calculation, a duration grammar, a record coercion lives. So core is tier 0/1 (§9): pure
computation, at most reading a path it was handed. No network, no subprocess, no dynamic
execution, no secrets.

The last one enforces itself, for free. `secrets_store._calling_helper()` walks out to the
first frame whose module isn't the store's own and requires it to be a `codeact_helper_*`
module; a `codeact_core_*` frame asking for a secret is denied today, with no new code.
Worth writing down as intended rather than leaving it as a happy accident someone later
"fixes".

### Core has no contract of its own

A helper's documentation is trustworthy because its examples are executed and their real
output captured (§5). Core has no examples, because it has no card. So what stops core
rotting?

Its dependents. **Core is verified through the helpers that use it**, and that's the
honest description of its status rather than a gap in it: core has no external contract,
so there's nothing to verify except the behaviour its callers depend on — which is exactly
what their captured examples pin. A core change with no dependent example covering it is
uncovered, and the review app can say so out loud, because it knows the graph.

This is also why core needs no approval ceremony of its own. Approving a core change means
re-capturing every dependent's examples; the diff a human reads is the source, the list of
dependents, and what those dependents now print. If nothing printed differently, the change
was safe in the only sense the system can check.

### The closure hash

This is the mechanic the whole section rests on.

Quarantine fires today when a helper file's `source_hash` stops matching the hash recorded
in its sidecar at capture time — "the source changed since its examples were last verified"
(§6). With an edge, editing core changes *no helper file*, so every dependent's card
silently becomes unverified while continuing to claim it was checked. That's the exact
failure the quarantine mechanism exists to prevent, arriving through the back door.

So the sidecar records a **closure hash** instead: the helper's own source plus the source
of every core module and helper it imports, transitively, hashed in name order.

```
  edit core/records.py
       │
       ├──► closure hash of read_jsonl  changes ──► read_jsonl  quarantined
       ├──► closure hash of write_jsonl changes ──► write_jsonl quarantined
       └──► codeact check --capture
                 ├─ examples produce what the cards documented ──► sidecars re-stamped,
                 │                                                 quarantine lifted
                 └─ anything differs ─────────────────────────────► stays quarantined,
                                                                    and that is the
                                                                    regression report
```

One cheap property worth keeping: the hash is over sorted `(name, source)` pairs, so it's
stable across load order and across a dependency being reached by two paths.

### Composition: a helper calling a helper

An edge between two helpers is declared — `@helper(..., uses=["read_jsonl", "group_by"])` —
and **verified against the AST**, both directions. Called but not declared is hidden reach.
Declared but not called is a stale card. The gate reports both; the AST pass is the check,
and the declaration is the artifact a human reviews.

**Effect closure is the rule that matters most.** A helper's effective reach is its own
declared effects plus the effects of everything it uses, transitively; the gate checks the
*effective* value against the job, and the card shows where it came from:

```
job: transform | domains: data | side effects: network (via fetch_url)
```

Without that, composition is a laundering path for reach: wrap `fetch_url` in a function
declaring `side_effects="none"`, and the pure/world-touching boundary the job table exists
to hold is gone — quietly, because the wrapper's own body contains nothing suspicious. A
`transform` that calls an `acquire` is not a transform, and the gate should say so in those
words.

**The capability delta has to compare closures too.** `_capability_delta` currently diffs
declared sets, so a revision whose entire change is one added line — `uses=["gh_pr_fetch"]`
— would gain network access and a secret while reporting `escalates: false`. That is
precisely the edit §6 calls the most dangerous in the system, arriving in the one form the
existing check can't see. Effective sets, or the check is decorative.

**Delegation runs under the callee's approval, not the caller's.** If A uses B and B
declared `GITHUB_TOKEN`, the store's frame walk finds B's module and allows the read —
correctly. A never sees the value, and can only invoke B's published contract, which is
exactly what it could have done by calling B directly. The approved pairing was
`(GITHUB_TOKEN, B)` and it stays `(GITHUB_TOKEN, B)`.

**Cycles are refused** at the gate, naming the path, rather than left to surface as an
import error from inside somebody's example run.

**No depth limit.** The constraint that matters is visibility, not depth — a reviewer
approving a helper is approving everything it can reach, so the card and the review screen
show the full transitive closure and the trial run reports it as its own line rather than
burying it in file reads. A rule capping depth at two would be arbitrary and would push
authors into flattening by copy-paste, which is the thing this section exists to stop.

**Quarantine does not cascade**, and this is worth stating because the instinct is that it
should. Quarantine means *this card's documented behaviour may be false*. A dependent
doesn't read that card; it calls the code. Its own captured examples are what verify it,
and if those still pass it is exactly as trustworthy as it was yesterday. Cards are for
readers, code is for callers — and the code half is already covered by the closure hash,
which fires on a source change whether or not anybody quarantined anything. The review app
should still mark "depends on a quarantined helper", because it's a strong hint about where
to look next. It is not itself a defect.

### Composability at the call site

The other half of "functions should be composable" isn't about the library's internal graph
at all. It's whether the agent can chain two helpers without writing glue between them —
a constraint on *signatures*, which today holds implicitly and undocumented: `read_jsonl`
returns `list[dict]`, `group_by` and `diff_records` consume `list[dict]`,
`to_markdown_table` consumes `list[dict]` and returns `str`. They compose because they
happen to agree on a record shape nobody wrote down.

Write it down. The job axis already implies the order; the shapes are what travel along it:

```
   outside                                                                     reading
      │                                                                            ▲
   acquire ──text/bytes──► parse ──records──► transform ──records──► present ──text┘
      │                      │                   │  │
      │                      └───────────────────┤  └──► generate ──artifact──► mutate ──► outside
      │                                          ▼
      └────────────────────────────────────► inspect ──scalar/report──► reading
                                            (answers, changes nothing)

   orchestrate ── wraps any span of the above, and inherits whatever it wraps
```

The shape vocabulary is small and closed for the same reason the job vocabulary is:
`path`, `text`, `records`, `record`, `mapping`, `table`, `tree`, `scalar`, `any`. And it's
**derived from the type annotations rather than declared** — `list[dict]` is `records`,
`str | Path` is `path` — with a declaration available only as an override, because a third
hand-written taxonomy is a third thing to get wrong.

Two things fall out, and only one is new machinery. The gate gains a free composition
warning: a `transform` that consumes `records` and produces something outside the lattice
should explain itself in review. And retrieval gains a genuinely strong signal —
`search_helpers(task=..., have="records", want="table")`. Domain failed as a hard filter
(§14, question 5) because it was a judgement call the model had to guess right; shape is
derived from code, so it doesn't have that failure mode. It still earns its way up the same
ladder: ranking boost first, hard filter only if the retrieval eval says it can be.

### Compositions accumulate as `orchestrate` helpers

This answers question 7 below by deleting the subsystem it asks for. A recurring path —
"for this job, run `a()` then `b()` then `c()`" — that's worth keeping is a function whose
body is those three calls. Give it a card and `uses=["a", "b", "c"]` and it is an
`orchestrate` helper, indistinguishable from one written by hand: same gate, same captured
examples, same effect closure (`inherits` already exists for exactly this), same review, same
retrieval path. No recipe object, no recipe store, no second thing to search.

What the miner gains is a second cluster type. It fingerprints inline code today (§6); a
repeated *sequence of helper calls* is visible in the same corpus blocks and is the
strongest candidate signal available, because every step is already a reviewed unit. The
only open question about such a cluster is whether the sequence deserves a name.

### What changes, by file

| where | today | with the edge |
|---|---|---|
| `registry.py` | loads each file standalone; a sibling import fails and the helper vanishes into `errors`, which only the CLI prints | core and helper directories on the import path; an import failure becomes a named broken entry that `describe_helper` can answer with, not silence |
| `registry.py` sidecar | `source_hash` of one file | `closure_hash` over the file plus every source it transitively imports |
| `helper.py` | `job`, `domains`, `side_effects`, `requires_secrets` | `+ uses=[...]` |
| `cards.py` `validate` | job vs. own side effects | job vs. **effective** side effects; card renders `network (via fetch_url)` and the `uses` closure |
| `cards.py` `build` | signature as text | `+ consumes`/`produces` derived from annotations |
| `proposals.py` `_capability_delta` | diffs declared sets | diffs effective closures, or adding a dependency is an invisible privilege increase |
| `proposals.py` gate | one file, one helper | `+ uses` vs. AST both directions, cycle refusal, core-purity check |
| `trial.py` | loads the candidate alone in a scratch dir | resolves its dependencies, and reports `depends on:` as a first-class line rather than as incidental file reads |
| `secrets_store.py` | caller must be `codeact_helper_*` | unchanged — and that's the point: core can't ask, delegation runs under the callee |
| `search.py` | job filter + BM25 + domain boost | `+ shape` boost |
| `miner.py` removal queue | "nothing has called it" | helper-to-helper calls count as calls, or every shared helper looks dead and gets retired out from under its callers |
| `miner.py` candidates | clusters of inline code | `+ clusters of repeated call sequences` → `orchestrate` proposals |

### What it costs

Three honest ones.

**The library stops being a set of independent files.** Today every helper is trivially
separable: one file, `git revert` it and the world is consistent. With edges, reverting one
file can leave a dependent calling something that no longer exists. The closure hash turns
that from a silent breakage into a quarantine, which is the best available outcome and
still strictly worse than the property being given up.

**Review gains a graph.** Approving a leaf helper means reading one file. Approving one
three edges deep means reading one file and trusting a closure that is displayed but not
re-read. That's the same trust move as any dependency in any package manager, and it is
genuinely weaker than what the design has today. Displaying the closure prominently is a
mitigation, not a fix.

**Core is a place for things to hide.** No card, not searchable, read only by whoever is
editing a dependent. Purity by rule and dependent-example review are the mitigations, but
the real one is size: core should be dozens of lines, not thousands. If core grows a
subsystem, that subsystem wanted to be a helper.

### Ordering

1. **Import path + broken entries made visible.** No new concepts; fixes a silent failure
   that already exists.
2. **Closure hash and the quarantine it drives.** Before any edge can be created, so the
   first one is verified from the moment it exists — the same reason corpus logging shipped
   in phase 1 ahead of anything that read it.
3. **The core layer**: directory, `codeact.core` resolution, purity check.
4. **`uses=`**: AST verification, effect closure, closure-aware capability delta.
5. **Shapes**: derived, rendered on the card, ranking signal, then the retrieval eval.
6. **Miner sequence clusters** — last, because it needs a corpus in which composed helpers
   have been getting called.

---

## 13. Suggested build order

| Phase | Ships | Why here |
|---|---|---|
| 1 ✅ | interpreter + `run_python` + skill, guard in **audit mode**, **corpus logging** | useful on its own; both logs start filling immediately, and neither is recoverable retroactively |
| 2 ✅ | registry, **card format + contract tests**, job/domain taxonomy, `search_helpers`, `describe_helper`, preloading, seed helpers, **both evals** | proves the retrieval ergonomics and card sufficiency against hand-written content, before any of it is load-bearing |
| 3 ✅ | `propose_helper` (incl. `revises`), validation gate, **revision flow + quarantine**, **review app v1** (card, source, diffs, approve/deny, run examples in a scratch dir), **guard enforcement on** | the actual twist — and enforcement only makes sense once there's a way to say yes to what it blocks |
| 4 ✅ | layer 2 containment (Landlock + seccomp), capability grants, **secrets** (store, wrappers, egress redaction), dependency install | the real boundary; trial runs and network helpers both become safe here, so this gates anything that touches credentials |
| 5 ✅ | **the miner** (fingerprint, cluster, rank, synthesize), the four queues in the app, side-effect reports, project affinity, promote/demote | makes accumulation self-sustaining — and by now there's a corpus worth mining |
| 6 | **the dependency edge** (§12): closure hash, the core layer, `uses=` with effect closure, shapes, sequence mining | the library has to be big enough for duplication between helpers to be a real cost before paying the price of coupling them |

**Phases 1–5 are implemented**, and layer 2 has a real answer: **`run_as` runs the
interpreter as a separate OS user**, which is enforced by the kernel rather than by an
AST walk. Two facts shaped it, both measured rather than assumed. An unprivileged parent
cannot use `subprocess(user=)` at all — it raises `PermissionError` without `CAP_SETUID`
— so the practical route is `sudo` with a tightly scoped `NOPASSWD` rule, and running the
server as root to avoid that would be worse than the problem. And `sudo` *does* relay
`SIGINT`, which is what lets the timeout escalation keep working: signalling across uids
is normally forbidden, and without the relay every timeout would have had to destroy the
session's state.

What this does not do is confine syscalls. Landlock and seccomp would add that, and
remain undone; `run_as` gives filesystem and process isolation, which is the part that
matters most for an interpreter that would otherwise be able to read `~/.ssh`.

Two other honest limits. Secrets are stored under filesystem permissions only; the
standard library ships no cipher, and rolling one would read as protection while
providing none. And the miner's synthesis step is manual: it surfaces ranked clusters
for a human to act on rather than writing the helper itself.

Phase 1 is a day. Phases 2–3 are where the design risk is. Four sequencing points worth
keeping: phase 2 uses hand-written helpers so we can measure whether task-driven discovery
works before building the machinery that generates the content; guard enforcement waits
for phase 3 because a wall with no door is just a broken tool, so the escalation path has
to exist before the blocking does; both the corpus and the guard's audit log start
recording in phase 1 despite nothing reading them until phases 3–5, because history is the
one thing that can't be backfilled; and the review app's Run button stays confined to a
scratch directory with no credentials until phase 4 ships real containment, because a
trial run of unreviewed code is the least safe thing in the system (§11).

---

## 14. Open questions

1. **How often should the miner run, and does it need a budget?** Batch review is now the
   default surface (§6 track 2 lands there), which settles the friction question, but not
   the cadence: nightly, weekly, or on an explicit `/codeact:mine`? The synthesis pass
   costs model calls proportional to cluster count, so it likely needs a cap on candidates
   per run — and a rule for what happens to clusters that keep getting deferred.
2. **Seed library.** Ship with a starter set (file/AST/HTTP/git utilities) so discovery
   has something to find on day one, or stay empty so everything is earned?
3. **Helper granularity.** ~~One file per helper is maximally reviewable but awkward for
   helpers that want to share private state. Allow modules of related helpers?~~
   **Answered in §12**, and not by allowing multi-helper modules — sharing goes to a
   separate `core` layer that has no cards and is never discoverable, because the thing
   that made this awkward was one file being both the unit of review and the unit of
   sharing. Split those and both get simple. What remains open is empirical: whether core
   stays as small as it has to be to remain reviewable.
4. **Where exactly do the tier boundaries fall?** Settled that the agent authors freely
   under sign-off (§9), but tier 0's stdlib list and the tier 1/2 line are guesses until
   audit mode produces real data. Specifically unresolved: is `subprocess` tier 2 when
   `Bash` is already granted at the Claude Code level, and does read access really extend
   to the whole project root or only to tracked files?
5. **Is the eight-job vocabulary right?** Partly answered by phase 2. Twelve hand-written
   helpers classified without strain, and the retrieval eval scored 100% hit@1 across a
   12-task gold set — but only after **domain stopped being a hard filter**. The helper
   that resisted was exactly the predicted one: `retry_call` sits in `orchestrate` and has
   no honest domain, since it wraps any callable, so a perfectly reasonable
   `domains=["http"]` guess excluded the one right answer that plain ranking put first.
   Jobs stay a hard filter; domain is now a ranking boost. `orchestrate` also needed a
   side-effect value of its own (`inherits`), because "none" was a lie for a helper whose
   purpose is invoking arbitrary caller code. Still open at thirty helpers rather than
   twelve.
6. **Does "breaking change means a new name" hold up in practice?** It's what lets the
   design skip versioning entirely (§6), and the reasoning is sound — a helper's callers
   are future sessions reading a fresh card, so there's nothing to migrate. The risk is
   name sprawl: `parse_log`, `parse_log2`, `parse_log_structured` accumulating because
   every real improvement was technically breaking. If that shows up, the removal queue is
   the pressure valve, but it may need to be more aggressive than "unused for months."
7. **Should compositions accumulate too?** ~~Individual helpers are the unit now, but the
   recurring thing is often a *sequence* — "for this job, the path is `a()` then `b()`
   then `c()`". Capturing those as recipes is a natural extension of the same flywheel,
   and probably a phase 5+ question rather than something to design in now.~~ **Answered
   in §12: yes, and as nothing new.** A recipe worth keeping is an `orchestrate` helper
   whose body is those three calls — same gate, same card, same retrieval. The recipe
   object was the wrong shape for the idea; what's real is a new *mining* cluster type,
   over repeated call sequences rather than repeated inline code.
8. **How do secrets get in?** `codeact secret set`, import from the environment, keyring
   passthrough — and what happens when a helper declares a secret that isn't set yet. The
   card should probably surface it as an unmet precondition rather than failing at call
   time with something opaque.
9. **When a helper is obviously project-specific, what then?** Project affinity (§7) keeps
   it from cluttering other projects' retrieval, but it's a ranking fix for a modelling
   gap. If this turns out to be common rather than rare, per-project libraries come back
   as a real requirement — the deferred sharing question (§2) returning by a different
   road.
