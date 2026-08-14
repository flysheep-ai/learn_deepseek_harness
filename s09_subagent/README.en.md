# s09 — Subagent

**中文版：[README.md](README.md)**

[s08](../s08_skill_loading/) → **s09** → [s10](../s10_context_compaction/) → … → s18

> The value of a subagent is **not** "one more LLM call".
>
> It is **Context Isolation**.

---

## The problem the last chapter left

The user asks:

> "Which files in this codebase handle authentication?"

The model will `grep`, `glob`, and `read` a dozen files. The search results —
6177 chars in the demo, 60,000 tokens in a real project — **all pour into the
main context and stay there forever**.

The final answer is one sentence. But the pile follows you through the whole
session:

- every later step re-sends it (**money**)
- every later step the model searches inside it (**attention**)
- the context overflows soon (s10's problem)

---

## What this chapter solves

Let the expensive exploration happen in **another context**; the main context
receives only the conclusion:

```
Main agent context                 Subagent context (independent, disposable)
┌────────────────┐             ┌──────────────────────────────┐
│ user: where's auth?│           │ task: find auth-related files  │
│ tool: spawn(…)  │──── spawn ─▶│ grep → 3000 chars              │
│ tool: "conclusion:│            │ read → 1500 chars              │
│   auth/mw.py:5  │◀── 100 chars│ read → 1200 chars              │
│   api/deps.py:3"│              │ "auth at mw.py:5, deps.py:3"   │
└────────────────┘             └──────────────────────────────┘
   grows by 4 messages             8 messages discarded with the child session
```

Demo measurements:

```
Control group (no subagent): main context 8 messages / 6177 chars
Test group (spawn explorer): main context 4 messages /  190 chars
Main context saved 5987 chars (97%)
```

---

## The new core concepts

### 1. The subagent has its own Session

```python
child = Session(path=parent_dir / f"{parent.id}_sub_{...}.jsonl")
```

This is the **physical implementation** of isolation. Its grep results, reads,
and detours all land in a different file; the main context's
`derive_messages()` never sees them.

Note the child session is **independently replayable** — isolation ≠ loss. The
parent log keeps only two bookkeeping records:

```
#10 subagent/start  {"child_session": "ses_823f…", "preset": "explorer", "task": "…", "tools": [...]}
#11 subagent/end    {"child_session": "ses_823f…", "steps": 4, "child_messages": 8, …}
```

To audit "what did that search actually look at", open the child log by
`child_session`.

### 2. A restricted action space

```python
def restricted(self, allowed: list[str]) -> "ToolRegistry":
    sub = ToolRegistry()
    for name in allowed:
        sub._tools[name] = self._tools[name]     # share the Tool object, don't copy
    return sub
```

This is s03's "the registry is the **single source** of the action space"
starting to pay its debt.

Because `schemas()` and `get()` come from the same dict, a filtered tool is
**simultaneously**:

- absent from the subagent's prompt (it doesn't even know the tool exists)
- unfindable at execution (guessing the name doesn't help)

The demo verifies it live:

```
Main agent:  bash, read, write, edit, glob, skill, grep, spawn_agent
explorer:    read, glob, grep
blocked:     bash, write, edit, skill, spawn_agent

Even if the model guesses the name "write":
  Error: no tool named 'write'. Available: read, glob, grep
```

> **Both sides consistent — that's what "restricted" means. Just not telling
> it is not a restriction.**

`explorer` structurally cannot modify anything — no reliance on "we told it
nicely in the prompt".

### 3. SubagentPreset: the harness draws boundaries, the model picks one

```python
SUBAGENT_PRESETS = {
    "explorer": SubagentPreset(tools=["read", "glob", "grep"], identity="You are a read-only explorer…"),
    "editor":   SubagentPreset(tools=["read", "glob", "grep", "edit", "write", "bash"], …),
}
```

The harness defines **capability envelopes**; the model decides **which
envelope for which job**.

The forbidden version:

```python
if "search" in task: spawn("explorer")     # ❌ the harness deciding for the model
```

**Why not let the model specify its own tool list?**

```python
spawn_agent(tools=["bash", "write"])     # ❌ the model granting itself permissions
```

Security boundaries are the harness's to draw. The model may only choose among
the envelopes already drawn.

### 4. The subagent inherits nothing from the parent's context

```python
child_rt = RuntimeContext(cwd=WORKSPACE, tool_names=child_registry.names(),
                          project_notes=None, skill_catalog=[])
child_prompts = SystemPromptRegistry()
child_prompts.register(PromptSection("identity", 10, lambda c: preset.identity))
```

It is a **newborn agent** that knows only the task. Hence the `task` parameter's
description says:

> "Describe the task fully. **It cannot see your conversation history.**"

That's the cost of isolation — and it must be stated explicitly to the model.

---

## Minimal architecture diagram

```
                   registry（8 tools）
                         │
       ┌─────────────────┴──────────────────┐
       │                                    │ restricted(["read","glob","grep"])
       ▼                                    ▼
   Main agent                          explorer subagent
  ├─ Session（parent）                  ├─ Session（child, separate file）
  ├─ prompts（identity/skills/…）        ├─ prompts（child identity only）
  ├─ RuntimeContext（skills/project/progress）├─ RuntimeContext（empty）
  └─ Inbox                             └─ Inbox（task only）
       │                                    │
       │ spawn_agent(agent, task) ─────────▶│
       │                                    │  run_turn(...)
       │◀────── outcome.text（conclusion only）────┘
       │
       ├─ parent log: subagent/start · subagent/end（log-only）
       └─ main context: exactly 1 tool result added
```

Note: `run_turn` **doesn't know who is a subagent** — it just received a
different registry, a different prompt registry, a different session.

---

## Run it

```sh
python s09_subagent/code.py --demo
python s09_subagent/code.py --demo --debug
```

The visual boundary in the output matters:

```
→ spawn_agent agent='explorer', task='…'
  ┌─ subagent[explorer] started  tools=read,glob,grep
    → grep pattern='def '
    → read path='auth/middleware.py'
    → read path='api/deps.py'
  └─ subagent[explorer] done  steps=4  child context 8 messages → returns 100 chars
  ✓ Auth is in auth/middleware.py:5 and api/deps.py:3
```

**Everything inside the box never enters the main context.** The main agent
only sees the final `✓`.

---

## Why it's designed this way

### Why this is more than "saving tokens"

Money matters, but attention matters more.

A model finding 3 key lines inside 60,000 tokens of context performs
differently from a model reading one sentence of 200. And those 60,000 tokens
compete for attention on **every subsequent step** — you don't pay once, you
pay N times.

Isolation decouples "the cost of exploration" from "the value of the
conclusion".

### Why a separate log file, not a section of the parent log

If stuffed into the parent log (even as log-only), every consumer must learn to
"skip events belonging to child sessions". That filter contaminates replay,
compaction, audit.

**One session = one log.** Reference it with `child_session` instead of
cramming two trees into one array.

### Why the return value is plain text, not structured data

```python
return outcome.text or "（the subagent returned no conclusion）"
```

Because it must enter the model's context, and the model's context is text.

Real systems have richer return protocols (subagents can call a `report` tool
with structured conclusions) — optimization, not essence. The essence: **a
subagent's output must fit in one message.** If it returns 5000 lines, the
isolation was wasted.

So the explorer's identity explicitly says:

> "Give file names and line numbers; **do not paste large excerpts**."

### Why spawn depth is limited

```python
MAX_SUBAGENT_DEPTH = 1   # subagents cannot spawn
```

Like s02's `MAX_STEPS`: **resource protection, not intelligence**. Unbounded
nesting burns money exponentially.

s16 relaxes this (Agent Teams need more levels) but replaces it with finer
budget control.

### Is this the harness thinking for the model?

No. Check the division of labor:

| Who | Does |
|---|---|
| Harness | provides `spawn_agent`; defines the explorer/editor envelopes; isolates context; restricts tools |
| Model | decides **whether** to spawn, **which** role, **what task** to give, how to use the conclusion |

The `subagents` prompt section describes **capabilities**, never "when to use
which":

```
- explorer: read-only exploration. Good for wide searches, reading code, locating problems.
- editor: read-write. Good for landing a well-defined change and self-verifying.
```

"Good for…" describes the tool's nature, the way `read`'s description says
"read file contents". It's not an `if`.

---

## What changed vs. the previous chapter

| | s08 | s09 |
|---|---|---|
| Context | one | **main / child, physically isolated** |
| Cost of a big search | permanently in the main context | **discarded with the child session** (demo: −97%) |
| Tool sets | identical for all | `registry.restricted()` per preset |
| Prompt | one global set | child uses its own `SystemPromptRegistry` |
| New tool | 7 | **8** (+`spawn_agent`) |
| New events | — | `subagent/start` `subagent/end`（log-only） |
| `run_turn` | — | one new `prompt_registry` parameter, **nothing else** |

That `run_turn` can be reused by the child directly is the payoff of s05–s07
pushing all state into parameters.

---

## What real systems do on top

- **Multiple providers**: DeepSeek Harness's `ctx.subagents` is a **registry of
  named providers** — the same spawn interface can back onto in-process
  subagents, forked processes, or even another product (Codex / Claude Code).
  We have one kind: in-process + fresh session.
- **Continuable subagents**: real subagents can stay alive; the parent sends
  follow-up messages (`send_message` / `interrupt_agent`). Ours are one-shot.
  s16 makes a simplified persistent version.
- **Scope and shadowing**: real registrations (tools, prompt sections,
  listeners) belong to a **scope**; on collision "nearest scope wins". That
  enables "give this one agent a variant of the read tool". We used the crudest
  approach: build a fresh registry.
- **Concurrent subagents**: several children run in parallel. We're serial.
- **Depth and lineage**: real systems carry `parentSession` / `delegationDepth`
  as **data**, not as scope structure. Our `subagent/start` records `parent`
  too.

---

## Try it yourself

1. **Tighten explorer by one more notch**
   Change its tools to `["read", "grep"]` (drop glob), run the demo, watch its
   prompt and execution change together.

2. **Add a preset**
   ```python
   "tester": SubagentPreset(
       name="tester", description="Only runs tests and reports results; changes no code.",
       tools=["read", "bash"], identity="Run tests and report failures. Do not modify any file.")
   ```
   **You changed not one line of `run_spawn`.**

3. **Quantify the isolation**
   Change `range(10)` to `range(100)` in `build_big_workspace` and compare the
   character counts again.

4. **Make the subagent chatty on purpose**
   Change explorer's identity to "report everything you read verbatim" and
   watch the main context's size climb back. Proves that isolation's effect
   depends on the **size of the return value**, not merely on "using a
   subagent".

5. **Read the child session log**
   The demo leaves `*_sub_*.jsonl` files in the temp workspace. Open one with
   s05's `--replay`. (The child session is a complete, independently replayable
   session.)

6. **Verify the restriction's consistency**
   ```python
   sub = registry.restricted(["read"])
   assert "write" not in [t["name"] for t in sub.schemas()]   # not in the prompt
   assert sub.get("write") is None                            # not at execution
   ```

---

## Next chapter

Subagents solve "**new** large content must not enter the main context". But
the main context itself keeps growing:

```
[step 1]  messages=1
[step 2]  messages=3
[step 3]  messages=5
...
[step 40] messages=79
```

A half-hour session will hit the model's window limit.

The bluntest fix is truncation:

```python
messages = messages[-20:]      # keep only the last 20
```

**This explodes immediately.** Entry #20 might be a `tool_result` whose paired
`tool_call` sits at #19 — cut off. The model-side API errors out:
"tool_result has no matching tool_use".

And truncation means **forgetting**: the model no longer knows what it did 20
steps ago and repeats itself.

Is there a way to shorten the context **without** losing history and **without**
breaking pairings?

And if s05 said "the log is the only truth, messages are just a projection" —
what exactly does compaction compact?

→ [s10 — Context Compaction](../s10_context_compaction/)
