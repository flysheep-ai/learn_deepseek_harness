# s07 — Prompt Assembly

**中文版：[README.md](README.md)**

[s06](../s06_turn_and_step/) → **s07** → [s08](../s08_skill_loading/) → … → s18

> Is the system prompt a constant, or a **runtime artifact**?

---

## The problem the last chapter left

s06's system prompt looks like this:

```python
def make_system(cwd, reg):
    return (f"You are a coding agent at {cwd}.\n"
            f"Available tools: {', '.join(reg.names())}.\nAct, don't explain.")
```

139 characters — fine. But the coming chapters want to stuff in:

| Chapter | Content | Needed when |
|---|---|---|
| s08 | the skill catalog | always |
| s09 | subagent capability descriptions | only for the main agent |
| s11 | the current task list | when there are tasks |
| s12 | background job status | when jobs are running |
| — | project conventions (AGENTS.md) | when the file exists |

Keep concatenating into that f-string and two consequences arrive:

**1. It becomes everyone's edit point.** Adding a skill edits it, adding a tool
edits it, adding a task system edits it again. A 200-line string nobody dares
touch.

**2. It can't say "not this time".** No AGENTS.md? Still print "project
conventions: none". No tasks? Still print "current tasks: none".
**Every request pays for content that doesn't exist.**

---

## What this chapter solves

Split the monolith into **registrable, ordered, conditionally-skippable**
blocks:

```
PromptSection(identity)      ┐
PromptSection(environment)   │
PromptSection(project)       ├─▶ assemble(ctx) ─▶ system prompt
PromptSection(tools)         │        ▲
PromptSection(session_state) ┘        │
                                RuntimeContext
```

And add an industrial invariant: **write the assembled result into the log**
(the `request/header` event), so "every model request" is reconstructable in
full.

---

## The new core concepts

### 1. PromptSection: three fields are enough

```python
@dataclass(frozen=True)
class PromptSection:
    name: str                                        # identity; same name = replacement
    order: int                                       # sort weight
    render: Callable[[RuntimeContext], str | None]   # None = "not this time"
```

**`render` returning `None` is the whole point of the chapter.**

```python
@prompts.section("project", 30)
def _project(ctx) -> str | None:
    if not ctx.project_notes:
        return None          # no AGENTS.md? this block vanishes entirely.
    return f"# Project conventions\n{ctx.project_notes.strip()}"
```

> The prompt is not template-filling; it is **content selected by current state**.

Part 1 of the demo shows the contrast directly:

```
Cold start (no AGENTS.md)       222 chars
  identity       34
  environment    94
  project         -   (absent this time)
  tools          94
  session_state   -   (absent this time)

Same workspace with AGENTS.md   292 chars
  project        70   ← appears
```

**Same section set, different runtime state, different prompt.**

### 2. RuntimeContext: the only thing sections may read

```python
@dataclass
class RuntimeContext:
    cwd: Path
    tool_names: list[str]
    turn: int = 0
    step: int = 0
    project_notes: str | None = None
    files_read: list[str] = field(default_factory=list)
```

Sections may read **only this** — no globals. The constraint buys testability:
give a fake `RuntimeContext` and assert what a section renders, no agent
started.

It grows with the chapters (s08 adds skills, s11 tasks, s12 jobs) — but what
grows is **data**, never the logic of `assemble()`.

### 3. SystemPromptRegistry: twenty-something lines

```python
def assemble(self, ctx) -> str:
    parts = []
    for sec in sorted(self._sections.values(), key=lambda s: s.order):
        text = sec.render(ctx)
        if text:
            parts.append(text.strip())
    return "\n\n".join(parts)
```

Its value is not the logic; it's that **"who may add to the prompt" becomes a
registration act**.

Part 4 of the demo adds a block at runtime without touching one existing line:

```python
@prompts.section("safety_note", 60)
def _safety(c): return "# Note\nThis environment forbids network access."
```

With s14's plugin system, this is exactly how plugins contribute prompts —
nobody edits anyone else's code.

### 4. request/header: the request itself must be reconstructable

```python
if system != last_header:
    session.append(EV_REQUEST_HEADER, {
        "turn": turn, "step": step,
        "system": system,
        "tools": [t["name"] for t in tools],
        "sections": [n for n, size in prompts.explain(rt) if size],
    })
```

s05 said "anything that reaches a model request must be reconstructable from
the log". But s05/s06 only logged messages — **the prompt and the tool list
were not logged**. The log could reconstruct what the model *said*, but not
what the model *was told*. The replay was incomplete.

Now it's fixed — and only **on change**:

```
request/header snapshots in the log: 2 (this turn ran 4 steps)
  # 5 step 1  sections=identity,environment,project,tools           298 chars
  #13 step 2  sections=…,session_state                              338 chars
```

Steps 2/3/4 assembled the identical prompt, so only one snapshot. Full logging
is noise; no logging leaves the log incomplete; **log on change** is the
intersection.

---

## Minimal architecture diagram

```
            RuntimeContext
       ┌────────┴─────────────────────┐
       │ cwd  tools  turn  step        │
       │ project_notes  files_read     │
       └────────┬─────────────────────┘
                │
    ┌───────────▼────────────┐
    │ SystemPromptRegistry   │   sort by order
    │   10 identity          │   render(ctx) block by block
    │   20 environment       │   None → skipped
    │   30 project      ←conditional│
    │   40 tools             │
    │   50 session_state ←conditional│
    └───────────┬────────────┘
                │ assemble()
                ▼
         system prompt ──┬──▶ provider.chat(system=…)
                         │
                         └──▶ session: request/header（on change）
```

---

## Run it

```sh
python s07_prompt_assembly/code.py --demo
python s07_prompt_assembly/code.py --demo --show-prompt   # print the full prompt
python s07_prompt_assembly/code.py --demo --debug
```

Under `--debug`, watch the `system=` number:

```
[step 1]  → model request   messages=1 tools=6 system=298chars
[step 2]  → model request   messages=3 tools=6 system=338chars   ← it grew
[step 3]  → model request   messages=5 tools=6 system=338chars
```

After step 1 the model read `app.py`; the `session_state` section activated and
the prompt grew 40 chars by itself.

**Tool execution changed ctx, ctx changed the prompt, the prompt changed what
the model sees.** That chain is the most direct evidence that the prompt is a
runtime artifact.

---

## Why it's designed this way

### Why re-assemble every step, not every turn

Because sections like `session_state` change **within one turn** — a file was
just read; the next step should know.

Assembly is a pure function (result depends only on `ctx`), so it's cheap,
testable, predictable. The cost of recomputing is far below the bugs of "cache
+ invalidation" — the same reason s05 re-derives messages every step.

### Why session_state doesn't include the step number

```python
return f"# Progress (turn {ctx.turn})\nRead this turn: …"   # no step
```

Include the step number and the prompt becomes **different every step**. Two
consequences:

1. `request/header` snapshots lose their dedup; the log fills with near-identical prompts
2. provider-side prompt caches all miss (real money in production)

Put in only what the model actually needs. **"Can add" is not "should add"** —
every character in the prompt costs money and dilutes attention.

### Why tool **parameters** don't go into the prompt

```python
return f"# Tools\nAvailable: {', '.join(ctx.tool_names)}\nPrefer read over bash cat; …"
```

"How to use", not "what parameters". Parameters live in the tool schema, sent
separately by the provider.

Repeating them in the prompt is a common waste — and the two copies drift, the
same disease as s03's schema/implementation drift.

### Why sections have names, not just a list of functions

`name` makes a section **replaceable and removable**:

```python
prompts.register(PromptSection("identity", 10, my_custom_identity))   # override
prompts.remove("safety_note")
```

s14's plugin unload must withdraw its sections; s09's subagent needs a totally
different `identity`. Without names, neither is possible.

---

## What changed vs. the previous chapter

| | s06 | s07 |
|---|---|---|
| system prompt | one f-string function | **registry + ordered sections** |
| "not needed this time" | impossible | `render` returns `None` |
| Who may add content | edit `make_system()` | `@prompts.section(...)` |
| Assembly timing | once per turn | **once per step** |
| Prompt in the log | no | `request/header`（snapshot on change） |
| New concepts | — | `PromptSection` / `RuntimeContext` / `SystemPromptRegistry` |
| `run_turn` parameter | `system: str` | `rt: RuntimeContext` |

---

## What real systems do on top

- **Scope**: real harness sections can be **global** or owned by exactly one
  agent. On name collision, "nearest scope wins" — that's how per-agent
  personas are implemented. s09 uses a simplified version.
- **Waterfall interception**: assembly itself is an interceptable event;
  listeners can rewrite the final prompt. s13 gains this ability.
- **Prompt caching**: real systems mark the stable prefix for provider caching.
  That's the production value of "don't put per-step-changing content in the
  prompt".
- **Token budgets per section**: sections compete for limited tokens; real
  systems cap each block and truncate by priority. We concatenate in full.

---

## Try it yourself

1. **Add a section**
   ```python
   @prompts.section("git_state", 25)
   def _git(ctx):
       import subprocess
       b = subprocess.run(["git", "branch", "--show-current"], cwd=ctx.cwd,
                          capture_output=True, text=True).stdout.strip()
       return f"# Git\nCurrent branch: {b}" if b else None
   ```
   Note `order=25` slots it between environment and project — **you edited no
   existing code**.

2. **Make a block vanish**
   Change the demo workspace to `with_notes=False`, watch `project` show
   "absent this time" and the total drop by 70 chars.

3. **Make the prompt change every step on purpose**
   Put the step number back into `session_state`, re-run, and count how many
   `request/header` snapshots appear. That's the real cost of volatile prompt
   content.

4. **Test a section**
   ```python
   ctx = RuntimeContext(cwd=Path("/tmp"), tool_names=["read"], project_notes="X")
   assert "X" in _project(ctx)
   assert _project(RuntimeContext(cwd=Path("/tmp"), tool_names=[])) is None
   ```
   No agent, no model call — that's what "sections read only ctx" buys.

5. **See the actual prompt**
   ```sh
   python s07_prompt_assembly/code.py --demo --show-prompt
   ```

---

## Next chapter

Now suppose you want the agent to know some specialized knowledge:

- the team's git workflow (when to branch, how to write commit messages)
- the project's Python rules (ruff, type annotations, test layout)
- the correct way to run database migrations

The obvious move is to add sections:

```python
@prompts.section("git_guide", 60)
def _git_guide(ctx): return open("guides/git.md").read()      # 3000 chars
```

And then you notice: **every request pays for it.** The user asks "what does
this function do" — nothing to do with git workflows — and those 3000 chars go
out anyway. Ten such blocks = 30,000 chars per request.

And of those 30,000, the model probably needs 500.

Can we first tell the model **what knowledge exists**, and load the full text
only when it actually needs it?

→ [s08 — Skill Loading](../s08_skill_loading/)
