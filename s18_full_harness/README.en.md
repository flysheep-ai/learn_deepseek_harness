# s18 — Full Harness (Integration & Acceptance)

**中文版：[README.md](README.md)**

[s17](../s17_goal_loop/) → **s18**（the finish line）

> This chapter adds **no new mechanism**.
> It assembles s01–s17 into one complete machine, then verifies against the
> standard set at the start of the course.

---

## What this chapter solves

The previous 17 chapters each stand alone. Two questions remain:

1. Do they **fit into one machine** and cooperate?
2. Does that machine pass the **final acceptance test**?

```
User input: "Help me find out why the tests are failing and fix them."
```

The harness never learns that this is a debugging task:

- no hardcoded `run_test()` / `analyze_error()` / `modify_code()` steps
- no "if the user mentions tests, do X" branch
- only `read / write / edit / grep / glob / bash`,
  plus context / state / permission / session

The path the model walked on its own (demo transcript):

```
glob("**/*.py")                      ← look at the project shape first
bash("python3 tests/test_maths.py")  ← run the tests, see the failure
read("calc/maths.py")                ← read the implementation
read("tests/test_maths.py")          ← read the expectation
edit("calc/maths.py", …)             ← fix it
bash("python3 tests/test_maths.py")  ← verify, passing
```

**None of those six steps was written by the harness in advance.**
Each was produced by the model after observing the previous step's result.

---

## The integrated architecture

```
                 User
                  │
                  ▼
                Agent
                  │
           ┌──────┴──────┐
           ▼             ▼
       Session         Context
           │
           ▼
       Event Log
           │
           ▼
       Agent Loop
           │
           ▼
     Prompt Assembly
           │
           ▼
          LLM
           │
           ▼
       Tool Calls
           │
           ▼
 Tool Execution Pipeline
           │
 ┌─────────┼─────────┐
 ▼         ▼         ▼
Policy   Events   Tool Registry
           │
           ▼
  Capability Providers
           │
 ┌─────────┼─────────┐
 ▼         ▼         ▼
FS       Shell     Sandbox
```

The machine's numbers in the demo:

```
16 plugins: capabilities · identity · core-tools · session-log · validation
          · trace · redact · truncate · permission · timing · skills
          · tasks · jobs · subagent · compaction · goal
16 tools / 10 prompt sections / 9 services / 12 listeners / 67 events
```

s14's "the harness has no features of its own" reaches its final form here:
**every mechanism is an unloadable plugin, or a removable listener.**

---

## What counts as passing

| Criterion | Source |
|---|---|
| Completes the task autonomously | the model walked glob → bash → read → read → edit → bash |
| The harness doesn't know the task type | scanning the harness core for `if task_type` / `call_xxx_agent` / `router.route` — **zero hits** |
| Every step comes from observation | the model saw the previous result before each tool call |
| Fully traceable | 67 events on disk; replayable at any moment |
| Offline verifiable | `--demo` needs no API key |

With a real model:

```sh
python s18_full_harness/code.py
> Help me find out why the tests are failing and fix them.
```

(Run this inside a genuinely broken project — the agent walks the whole way,
including mid-course corrections when it hits walls.)

---

## Why it's designed this way

### Why "integration" produced no new code

s18's harness core is **identical** to s17's. Integration isn't "write a new
system" — it's "use the 17 pieces in order":

```python
h.use(CapabilityPlugin(...))       # s15
h.use(IdentityPlugin())            # s07
h.use(CoreToolsPlugin())           # s03 + s15
h.use(SessionLogPlugin())          # s05
h.use(ValidationPlugin())          # s03
h.use(PermissionPlugin(...))       # s04
h.use(TimingPlugin())              # s13
h.use(SkillPlugin(...))            # s08
h.use(TaskPlugin())                # s11
h.use(JobPlugin(...))              # s12
h.use(SubagentPlugin(...))         # s09 + s16
h.use(CompactionPlugin(...))       # s10
h.use(GoalPlugin(...))             # s17
```

That fact is s14's final proof: **adding a mechanism = adding a plugin;
existing plugins stay untouched.**

### Why some mechanisms went "unused" this time

The demo's model did **not** use subagents, task_write, bash_background, or
skills.

That's not a shortcoming — it's **correctness**:

- the harness offered those capabilities (visible in the prompt, callable)
- the model judged this task didn't need them
- so they sat quietly to the side

Had the harness forced "plan → spawn subagent → update tasks → run tests in
background", it would be scripting the workflow for the model again.

**"Unused" is the normal result of the Model-decides law.**

### Why the final check scans for code **shapes**, not bare words

```python
patterns = [r"if\s+" + t, "call_research" + "_agent", ...]
```

Bare-word searches hit the check code itself ("task_type" appears in the check
logic), and "searching words" misses the real violation — the real violation is
the shape of a **decision branch**:

```python
if task_type == "research": ...       # a shape — catch it
task_type = "research"                # an assignment — irrelevant
```

---

## Run it

```sh
python s18_full_harness/code.py --demo
python s18_full_harness/code.py --demo --debug
python s18_full_harness/code.py       # full acceptance with a real model
```

Five demo acts: composition → acceptance → the fixed result → mechanism
inventory → the iron-law scan.

---

## What changed vs. the previous chapter

| | s17 | s18 |
|---|---|---|
| Harness core | — | **identical to s17** |
| Demo | the three-round goal lifecycle | **autonomously fixing failing tests** |
| Acceptance | — | the standard set at the course's start |
| Check | — | scanning for decision-branch shapes |

---

## What real systems do on top

What separates this ~800-line core from DeepSeek Harness (7400+ files)?

- **Streaming**（`assistant/chunk` logged per token for replay）
- **Parallel tool execution**（`isConcurrencySafe` classification + rolling pools）
- **Real sandboxes**（Landlock / seccomp, not provider-side path checks）
- **Multi-provider registries**（the subagent seam mounts several named providers）
- **Configuration-driven assembly**（profiles as YAML, not elif）
- **Credentials / telemetry / title generation / attachments / LSP / terminals / web UI**

But note: **none of those is a new concept.**
They are all the 18 chapters' concepts repeated at engineering scale.
The course has done its job — now every page of dsh's documentation maps to a
chapter here.

---

## Try it yourself

1. **Make the acceptance harder**
   Bury two bugs in `build_buggy_project` (the divide-by-zero plus another
   failure elsewhere) and watch the model adjust on its own.

2. **Run the acceptance under another profile**
   `--profile minimal` — no tasks, no team, no compaction.
   Can the model still finish? Which mechanisms are "essential" and which are
   "nice to have"?

3. **Run the acceptance in another world**
   Swap `fs` for `MemoryFileSystem` and watch what the acceptance task becomes.

4. **Unload a plugin you consider irrelevant**
   Say `TruncatePlugin`, then produce a tool call with a huge output.

5. **Read s18's session.jsonl**
   67 events — walk through them naming which chapter's mechanism each one
   belongs to.

---

## After the finish line

Back to the sentence at the course's start:

> The agent's intelligence comes from the model. The harness's value is not to
> script the model's thinking, but to build the model an operable world of
> tools, environment, context, state, permissions, and feedback.

After 18 chapters you should be able to answer:

- why the Agent Loop is just a `while`
- why messages is not the truth
- why permission lives on the pipeline, not in the loop
- why compaction shadows instead of deleting
- why "everything is a plugin" is about boundaries
- why swapping a provider equals swapping the world
- why the harness provides cooperation mechanisms but never strategies
- why a goal is persistent state, not a `while not done`

**Suggested next step**: open [deepseek-harness's docs](https://github.com/deepseek-ai/deepseek-harness/tree/main/docs) —
every page will now be in conversation with these 18 chapters.
