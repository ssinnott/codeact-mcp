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
    ├── cards.py          # compile + validate usage contracts, run contract tests
    ├── validate.py       # the proposal gate
    └── telemetry.py      # usage counts, repeat-pattern detection
```

Helpers do **not** live in this repo. They accumulate in the *consuming* project:

```
<user project>/.codeact/
├── helpers/<name>.py        # code + its card, one file — reviewable, diffable, tracked
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
  ergonomics as discovering MCP tools. See §7 for how the slice is chosen.
- `describe_helper(name)` → the **card**: the usage contract, *not* the source. See §5.
- `propose_helper(name, source, card, job, domains, side_effects)` → runs the
  validation gate, writes a **pending** proposal. Installs nothing.

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
only for the revision workflow: the agent proposing a change to an existing helper does
legitimately need to see it. It is not part of the discovery path.

Debugging deliberately does *not* qualify. If a helper fails in a way its card didn't
predict, the correct outcome is a flagged helper and a human fix, not an agent reverse
engineering the body and routing around the damage. Telemetry already tracks failure
rates (§10); an unexplained failure should feed that, not be silently absorbed.

---

## 6. The accumulation loop

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

**Promote / demote.** Approval has two scopes: **session** (provisional — usable now,
disappears at session end) and **permanent** (written to `.codeact/helpers/`, git-tracked).
Telemetry drives movement: a provisional helper used successfully N times is offered for
promotion; a permanent helper that keeps raising exceptions gets flagged for revision.

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

### Does any of this actually work?

Categorization schemes are easy to design and hard to validate, so this needs numbers
attached early. Two evals, both cheap, both alongside phase 2.

**Retrieval hit rate.** Seed the library, write tasks where a specific helper is the right
answer, and measure how often the agent finds and uses it rather than rewriting the thing
from scratch. That tells us whether the job axis is carrying its weight or whether we're
building a filing cabinet nobody opens.

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
write to `.codeact/` itself. The agent must not be able to edit the guard, the approved
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

---

## 10. Suggested build order

| Phase | Ships | Why here |
|---|---|---|
| 1 | interpreter + `run_python` + skill, guard in **audit mode** from day one | useful on its own; audit logging starts collecting the data that sets the tier boundaries |
| 2 | registry, **card format + contract tests**, job/domain taxonomy, `search_helpers`, `describe_helper`, preloading, seed helpers, **both evals** | proves the retrieval ergonomics and card sufficiency against hand-written content, before any of it is load-bearing |
| 3 | `propose_helper`, validation gate, approval surfaces, **guard enforcement on** | the actual twist — and enforcement only makes sense once there's a way to say yes to what it blocks |
| 4 | layer 2 containment (Landlock + seccomp), capability grants, dependency install | the real boundary, once the policy layer has proven its tiers |
| 5 | telemetry, repeat detection, ranking priors, promote/demote, user scope | makes accumulation self-sustaining |

Phase 1 is a day. Phases 2–3 are where the design risk is. Two sequencing points worth
keeping: phase 2 uses hand-written helpers so we can measure whether task-driven
discovery works before building the machinery that generates the content, and guard
enforcement waits for phase 3 because a wall with no door is just a broken tool — the
escalation path has to exist before the blocking does.

---

## 11. Open questions

1. **Approval friction.** Both surfaces exist (inline elicitation vs batched
   `/codeact:review`) — which is the default, and does inline approval interrupt the
   agent's flow badly enough that batching should win?
2. **Seed library.** Ship with a starter set (file/AST/HTTP/git utilities) so discovery
   has something to find on day one, or stay empty so everything is earned?
3. **Helper granularity.** One file per helper is maximally reviewable but awkward for
   helpers that want to share private state. Allow modules of related helpers?
4. **Where exactly do the tier boundaries fall?** Settled that the agent authors freely
   under sign-off (§9), but tier 0's stdlib list and the tier 1/2 line are guesses until
   audit mode produces real data. Specifically unresolved: is `subprocess` tier 2 when
   `Bash` is already granted at the Claude Code level, and does read access really extend
   to the whole project root or only to tracked files?
5. **Is the eight-job vocabulary right?** It's a guess, and the honest way to find out is
   to categorize thirty real helpers by hand and see which ones resist classification or
   land in `orchestrate` because nothing else fit.
6. **How does revision actually work?** Source is visible only for the revise-an-existing-
   helper path (§5), but the flow isn't designed: does a revision re-run the full gate,
   does it need re-approval when only the card changed, and what happens to a helper whose
   contract tests fail while a revision is pending?
7. **Should compositions accumulate too?** Individual helpers are the unit now, but the
   recurring thing is often a *sequence* — "for this job, the path is `a()` then `b()`
   then `c()`". Capturing those as recipes is a natural extension of the same flywheel,
   and probably a phase 5+ question rather than something to design in now.
5. **Sandbox default.** Tier 0 matches what Bash already permits and keeps setup at
   zero; tier 2 is defensible but adds Docker/uv as a hard dependency.
