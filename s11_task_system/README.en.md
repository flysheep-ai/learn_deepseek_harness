# s11 — Task System

**中文版：[README.md](README.md)**

[s10](../s10_context_compaction/) → **s11** → [s12](../s12_background_jobs/) → … → s18

> Should the plan live in the model's head, or in the harness's hands?

---

## The problem the last chapter left

Give the agent a big task:

> "Replace all prints with logging in this project, then run the tests to
> confirm nothing broke."

The model says: "Sure, three steps: ① replace prints in core.py ② replace
prints in cli.py ③ run tests."

Then it starts step one. Twenty steps later, s10's compaction ran twice.

**It forgot step 3.**

Because "I plan to do three things" only exists in one `assistant` message. It
is context — shadowed by compaction, diluted by later content.

s10's summary prompt says "retain what remains undone" — but that's **praying**
the model writes it right every time, not **guaranteeing** it.

---

## What this chapter solves

Move the plan from "a thing the model said" to "state the harness holds":

```
Plan in the model's head          Plan in the harness's hands
┌───────────────────┐          ┌──────────────────────────┐
│ "three steps:      │          │ ● [t1] replace core.py   │
│  ① replace core.py │  ✗ shadowed│ ● [t2] replace cli.py   │
│  ② replace cli.py  │   by      │ ◐ [t3] run tests ← deps │
│  ③ run tests"      │   compaction│                        │
└───────────────────┘          └──────────────────────────┘
   one assistant message          re-rendered into the prompt every step
```

Demo evidence:

```
This turn compacted 3 times, shadowing 15 messages
But task/write is log-only — compaction can't touch it:
  4 task/write snapshots in the log, all intact
  the current list re-renders from the last snapshot (#87)
```

---

## The new core concepts

### 1. Task: deliberately few fields

```python
@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str = "pending"          # pending / in_progress / completed / failed
    depends_on: tuple[str, ...] = ()
    note: str = ""
```

No priority, no assignee, no estimate, no due date.

**Every extra field is one more thing the model reads every step.** Those are
project-management fields, not agent fields.

### 2. Whole-table snapshots (`task/write`), not CRUD events

```python
def write(self, tasks: list[Task]) -> None:
    self.session.append(EV_TASK_WRITE, {"tasks": [t.to_json() for t in tasks]})

def current(self) -> list[Task]:
    snapshot = []
    for ev in self.session.events():
        if ev.type == EV_TASK_WRITE:
            snapshot = ev.data["tasks"]      # last write wins
    return [...]
```

Why not `task/created` + `task/updated` + `task/deleted`?

Because the snapshot's replay rule is one sentence: **take the last one**.

Fine-grained events need edge cases handled: "updated a nonexistent id",
"deleted then updated". And the complexity buys nothing — task lists are small;
rewriting the whole table is cheap.

The tool description states it plainly:

> "The complete list. This is **overwrite, not append** — tasks not listed
> disappear."

### 3. `task/write` is log-only — the whole point of the chapter

```python
SURFACE_EVENTS = {EV_USER_MESSAGE, EV_ASSISTANT_MESSAGE, EV_TOOL_RESULT}
# EV_TASK_WRITE is not in there
```

Because it's not a message:

- it doesn't participate in the projection → s10's compaction can't touch it
- it re-renders into the prompt every step (via the `tasks` section)

That's the entire technical meaning of "moving the plan from the model's head
to the harness's hands".

The section sits near the top of the prompt (`order=15`): it's the anchor of
"what we're doing now", read before environment/tools background.

### 4. The harness validates state consistency — never list content

The boundary to hold carefully this chapter:

| Who | What |
|---|---|
| **Harness** | duplicate ids / dependency on a missing id / dependency cycles / completing before prerequisites |
| **Model** | which tasks exist, how to split, what order, when to re-plan |

The demo deliberately triggers one validation failure:

```
✗ Error: task t3 marked completed, but its prerequisites t1, t2 aren't completed yet.
       List not updated; fix and resubmit the complete list.
```

Two details:

- **A failed validation writes nothing.** Make the model retry rather than let
  the state rot.
- The error is **actionable**: which task, what it depends on, what to do.

Contrast the out-of-bounds version:

```python
if len(tasks) > 5:
    return "Error: too many tasks, merge them"        # ❌ planning for the model
if not any(t.status == "in_progress" for t in tasks):
    tasks[0].status = "in_progress"                    # ❌ choosing the order for the model
```

---

## Minimal architecture diagram

```
   the model calls task_write(tasks=[...])
        │
        ▼
   TaskStore.validate()  ← duplicate ids? missing deps? cycles? premature completion?
        │
        ├─ fails ──▶ return an actionable error, **write nothing**
        │
        └─ passes
             │
             ▼
        session.append("task/write", {tasks})   ← log-only whole-table snapshot
             │
             │  （at the start of every step）
             ▼
        TaskStore.current()  ← take the last snapshot
             │
             ▼
        RuntimeContext.tasks
             │
             ▼
        PromptSection("tasks", order=15)  ──▶ system prompt
                                              ▲
                       s10's compaction cannot reach here ────┘
```

---

## Run it

```sh
python s11_task_system/code.py --demo
python s11_task_system/code.py --demo --debug
```

The key three beats in the output:

```
→ task_write  ✓ list updated: 3 tasks, 0 completed.
→ task_write  ✓ Error: task t3 marked completed, but its prerequisites t1, t2 aren't completed yet.
→ read core.py
⟲ compaction: 5 messages → 1 summary  664 → 467 tokens      ← compaction happened
→ edit core.py
→ task_write  ✓ list updated: 3 tasks, 1 completed.
...
Task list（harness state, not model memory）
  ● [t1] replace prints in core.py  // load() done
  ● [t2] replace prints in cli.py
  ● [t3] run tests  ← depends on t1, t2  // smoke ok
```

**Three compactions later, all three tasks are still there.**

---

## Why it's designed this way

### Why `TaskStore.current()` is not cached

```python
if TASKS is not None and prompt_registry is None:
    rt.tasks = TASKS.current()        # re-read every step
```

Exactly the reason s05 re-derives messages every step:

> **A cache is a second truth; it will eventually disagree with the log.**

A task list is a handful of entries; a log walk per step is negligible. (Real
systems add derived caches with invalidation — an optimization, not a second
truth.)

### Why the `note` field earns its place

```python
● [t1] replace prints in core.py  // load() done; helper_* still use print — follow-up cleanup
```

`note` carries the **key conclusion after finishing** or the **failure reason**.
It's the only field that carries *information* rather than mere *status*.

After compaction, "I changed load() but not helper_*" survives in exactly two
places: the summary (not guaranteed) and `note` (guaranteed).

### Why `failed` exists instead of three states

`pending / in_progress / completed` can't express "tried, couldn't".

Without `failed` the model has two options: leave the impossible task hanging
in `in_progress` (and stall), or quietly mark it `completed` (and lie to
itself).

**A state machine must express everything that really happens** — otherwise its
users are forced to lie.

### How this relates to s06's turn/step

Three different layers of "progress":

```
Task    what to do      across turns, persistent, defined by the model
Turn    one input's drain     harness structure
Step    one model request     harness structure
```

One task may span 5 turns, or finish inside 3 steps of one turn. They are not
finer/coarser versions of the same thing — they are **different dimensions**.

---

## What changed vs. the previous chapter

| | s10 | s11 |
|---|---|---|
| Where the plan lives | a thing the model said | **harness state** |
| Plan after compaction | survives by the summary's mercy | **guaranteed intact** |
| New event | — | `task/write`（log-only, whole-table snapshot） |
| New tool | 8 | **9** (+`task_write`) |
| New section | — | `tasks`（order=15, near the top） |
| Consistency | — | dependency + status validation; failures write nothing |
| Process restart | context recoverable | **the list recovers too**（from the log） |

---

## What real systems do on top

- **An even simpler shape**: DeepSeek Harness's `todo/write` has only `content`
  and a three-state `status` — no id, no dependencies — because whole-table
  overwrite needs no stable identity. We added `id` / `depends_on` to teach
  dependency validation; real products often cut them. **Cut what you can.**
- **Plan mode**: a collaboration mode where the model writes the plan first, a
  human approves, then execution starts. A policy layer on top of the task
  system.
- **Tasks + subagents**: hand one task to a subagent (s09) and backfill `note`
  on completion. s16 does something similar.
- **UI rendering**: real products render the list as an interactive checklist
  the human can check or edit — at which point `task/write` gains a second
  writer.

---

## Try it yourself

1. **Remove the tasks section**
   `prompts.remove("tasks")`, re-run (with a real model), and watch whether the
   model still remembers step 3 after compaction.

2. **Construct a cycle**
   ```python
   task_write(tasks=[
     {"id":"a","title":"A","status":"pending","depends_on":["b"]},
     {"id":"b","title":"B","status":"pending","depends_on":["a"]},
   ])
   ```
   → `Error: task dependency cycle: a, b`.

3. **Change validation to "auto-fix"**
   Make `validate` silently downgrade a premature `completed` to `pending`
   instead of erroring. Then ask: will the model ever learn it made a mistake?
   (**When the harness quietly edits the model's input, the model never
   learns.**)

4. **Add a field, then take it out**
   Add `priority` to `Task`, run once, measure how much the tasks section
   grows. Then ask: did the model do better?

5. **Verify persistence**
   ```sh
   python s11_task_system/code.py     # a few rounds, Ctrl-C
   # then a small script: Session.load(...) → TaskStore(...).current()
   ```

---

## Next chapter

Now the agent needs to run tests:

```python
bash("python3 -m pytest")
```

This project's tests take **5 minutes**.

For those 5 minutes the entire Agent Loop **freezes**:

- the model waits
- the user waits
- nothing else can happen

Meanwhile the model could be reading code and preparing the next edit.

Worse: s03 gave bash a 60-second hard timeout, so the command **never
finishes** — the model gets "timed out" and falls into a "retry" death spiral.

Tool calls are **synchronous** — call and wait for the result.
But some work is inherently **asynchronous**.

Can the harness offer "start it, go do something else, come back for the
result"?

→ [s12 — Background Jobs](../s12_background_jobs/)
