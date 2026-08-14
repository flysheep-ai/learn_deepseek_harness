# s17 — Goal Loop

**中文版：[README.md](README.md)**

[s16](../s16_agent_team/) → **s17** → [s18](../s18_full_harness/)

> Where should a goal like "fix this bug" live?
>
> If it lives only in the model's context, the turn's end is the real end.

---

## The problem the last chapter left

s16's agent has a team and can run long tasks. But:

```python
run_turn(...)          # done
# is the goal complete? — nobody knows, and nobody cares. Turn over = over.
```

A goal like "fix this bug" rarely fits one turn:

- the model edits, tests fail → **another round** is needed
- the model stalls (missing dependency, no permission) → someone should know
  "where it's stuck"
- the user closes the terminal for dinner → the goal should **survive** and
  resume on next boot

None of this exists today. The goal lives only in the model's context:

- nothing asks "done yet?" when a turn ends
- no budget — the model can burn money spinning forever
- the process dies, the goal dies with it

---

## What this chapter solves

Make the goal **persistent harness state**, and wrap the turn loop in an outer
evaluation loop:

```
Goal（persistent state, in the harness's hands）
┌───────────────────────────────────────────┐
│ statement: "fix divide's zero handling"   │
│ status:    active / paused / blocked /    │
│            complete                       │
│ round: 2/4 ← budget                        │
└───────────────┬───────────────────────────┘
                │ at the end of every turn
                ▼
     evaluate: is the goal done?
      ├─ complete → wrap up（record goal/complete）
      ├─ blocked  → stop, record why（stop burning money）
      └─ continue → inject "[goal not done] continue", start a new round
                    （round exhausted → blocked: budget）
```

The demo walks the full three-round lifecycle (round 1 didn't act → round 2
produced a syntax error → round 3 fixed and verified):

```
↻ goal continue（round 1/4）: the model only read files this round, changed nothing.
↻ goal continue（round 2/4）: the fix is half-done（syntax error）; needs re-verification.
● goal complete: divide now has zero protection, verified.
```

---

## The new core concepts

### 1. Goal is state, not a conversation

```python
@dataclass(frozen=True)
class Goal:
    statement: str
    status: str = "active"      # active | paused | blocked | complete
    round: int = 0              # how many rounds already auto-continued
    max_rounds: int = 5         # the budget
    reason: str = ""            # why blocked/complete
```

A different dimension from s11's Task:

```
Goal   the **outcome** the user wants  — has a budget, a lifecycle, gets evaluated
Task   the **steps** the model splits  — cross-turn progress checklist
```

One Goal usually spawns several Tasks; evaluating the Goal looks not at the
task list but at "did the user get the outcome".

### 2. Four statuses + a budget

```
active    in progress
paused    a human paused it (state exists; not demoed in the course)
blocked   stuck / budget exhausted — stop auto-continuing, wait for a human
complete  the evaluator judged it done
```

The budget (`max_rounds`) means:

> It is **resource protection**, not **intelligence**.
> The harness doesn't know how hard the task is; it only guarantees that
> "auto-continue" has a ceiling. Ceiling hit → `blocked: budget`, waiting for a
> **human** to decide whether to renew.

The same family as s02's MAX_STEPS and s09's MAX_SUBAGENT_DEPTH. With this
chapter, the three-tier budget is finally complete:

```
round   goal level     max_rounds（s17）
turn    input level    MAX_STEPS_PER_TURN（s06）
step    call level     (per-tool timeouts)
```

### 3. Evaluation is a model call, not an `if`

```python
reply = self.evaluator.chat(
    [{"role": "user", "content": f"Goal: {goal.statement}\n\nRecent work:\n{...}"}],
    system=EVALUATE_SYSTEM)
```

"Is the goal done" is **not** something a few harness `if`s can answer — it
needs task semantics. So give it to a model (the evaluator).

The harness runs only the **lifecycle rules**:

```python
if verdict == "done":      → goal/complete, stop bothering the model
elif verdict == "blocked": → goal/blocked（with reason）, stop burning money
else:                      → round+1, inject "continue", start a new round
```

And the verdict parsing is **conservative**:

```python
# wrong format → treat as continue (one more round), never as done (premature stop)
```

Evaluator unavailable → treat as blocked (neither fake success nor infinite
spend).

### 4. Everything is events; the goal survives process restarts

```
#18 goal/start     round=0
#34 goal/evaluate  round=0  verdict=continue
#35 goal/start     round=1
#51 goal/evaluate  round=1  verdict=continue
#52 goal/start     round=2
#82 goal/evaluate  round=2  verdict=done
#83 goal/complete
```

`GoalStore` follows the same pattern as `TaskStore`: **derived from the event
log, never stored twice**.

Close the terminal → `Session.load` → `GoalStore.current()` → the goal is
still there, with its rounds, budget, and status.

That's the minimal form of a checkpoint: **the log itself is the checkpoint.**

---

## Minimal architecture diagram

```
   the user sets a goal
        │
        ▼
   goal/start（active, round=0）
        │
   ┌────────────────────────────────────────┐
   │              Goal Loop                 │
   │                                        │
   │   ┌─ run_turn（s06's loop, unchanged）─┐│
   │   │  act → observe → …                 ││
   │   └──────────┬─────────────────────┘  │
   │              │ turn ends               │
   │              ▼                         │
   │   EVT_TURN_END → GoalPlugin listener   │
   │              │                         │
   │              ▼                         │
   │   evaluator (a model): done/blocked/continue │
   │        │        │              │       │
   │        ▼        ▼              ▼       │
   │   complete  blocked     round < max?   │
   │    wrap up  stop spend  ├─ yes → round+1│
   │                         │    inject "continue" │
   │                         └─ no → blocked │
   │                                        │
   └────────────────────────────────────────┘
```

Note: **run_turn unchanged by a single line.** The whole goal mechanism hangs
on s13's `EVT_TURN_END` observer — a direct beneficiary of the s13/s14
architecture.

---

## Run it

```sh
python s17_goal_loop/code.py --demo
python s17_goal_loop/code.py --demo --debug
```

With a real model:

```sh
python s17_goal_loop/code.py
> /goal resolve all TODO comments in this project
> get started
# after each turn, the evaluator judges continue / blocked / done
> /goal        # check status anytime
```

---

## Why it's designed this way

### Why not `while not done:`

The forbidden version:

```python
while not done:
    run_turn(...)
    if "tests pass" in last_text:        # ❌ judging goal completion for the model
        done = True
    elif error_count > 3:                # ❌ judging "stuck" for the model
        done = True
    elif steps > 100:                    # ✅ fine — that's a budget
        done = True
```

The first two `if`s are the harness reading **task content** to make judgments
— out of bounds.

The correct division:

```
The harness owns: goal persistence, status persistence, budget, stop conditions, checkpoint
The model owns: what to do next, whether to revise the plan, how to solve the problem
```

The evaluator (a model call) produces the "done / stuck / continue" semantic
judgment — the judgment lives where it can only live: in a model's output.

### Why verdict parsing is conservative

```python
v = word if word in ("done", "blocked", "continue") else "continue"
```

The two misjudgments have asymmetric costs:

- continue misread as done → **the goal stops unfinished**; the user returns to
  work not done
- done misread as continue → one extra round burned; the next evaluation can
  still correct it

So the failure direction is "continue". **When misjudgment costs are
asymmetric, lean toward the cheaper one.**

### Why the goal section appears only while active

```python
@ctx.section("goal", 14)
def _goal_section(rt) -> str | None:
    if g is None or g.status != "active":
        return None
```

After completion the block vanishes from the prompt (demo part 6 verifies:
0 chars).

A completed goal must stop occupying the model's attention — s07's "the prompt
is a runtime artifact" extended naturally: **goal state changes, the prompt
follows.**

### The evaluator sees only recent work

```python
msgs = derive_messages(ctx.session)[-24:]      # recent work only
```

Evaluation doesn't need the full history. Same direction as s10's compaction:
**every consumer of a long session trims its own view.** The model reads the
full context, the evaluator reads the tail, the compactor reads boundaries.

---

## What changed vs. the previous chapter

| | s16 | s17 |
|---|---|---|
| After a turn ends | that's the end | **something evaluates and continues** |
| Where the goal lives | the model's context | **persistent harness state** |
| Process restart | goal lost | **goal recovers from the event log** |
| Budget | only turn step caps | **round-level max_rounds** |
| New objects | — | `Goal` / `GoalStore` / `GoalPlugin` |
| New events | — | `goal/start` `/evaluate` `/blocked` `/complete`（log-only） |
| run_turn | — | **not a line changed**（hangs on EVT_TURN_END） |

---

## What real systems do on top

- **Activation is process-local**: in DeepSeek Harness the goal's state is
  durable, but "may auto-continue" is an in-process permission
  (armed/disarmed) — after resume a human must re-authorize before autonomous
  work resumes. A security design we simplified into "active = auto-continue".
- **Goal rounds are specially sourced turns**: in the same session, human turns
  don't consume the goal budget; only goal-driven continuation rounds count.
  We simplified.
- **Paused / interruption**: a human can pause a goal; turns continue, just no
  evaluation.
- **Ralph loop**: dsh also has a workflow where each round opens a **fresh
  child session** (Ralph) — a different policy from same-session goals. We
  teach only the latter.
- **Blocked reason codes**: real `reason`s carry machine-routable codes
  (e.g. `budget-exhausted`). Ours are human text.

---

## Try it yourself

1. **Script the evaluator to always say continue**
   Watch `max_rounds` exhaust, the goal turn `blocked: budget`, and the harness
   stop injecting "continue" — **the spending has a ceiling.**

2. **Feed the evaluator a malformed verdict**
   `scripted("I think it's about done")` — watch it get treated as continue.

3. **Replace the store with an in-memory variable**
   Change `GoalStore` to `self._goal = ...`, restart the process, and feel s05's
   rule: anything that must outlive the process must be reconstructable from
   the log.

4. **Add `/goal pause`**
   Introduce the `paused` status and have the turn-end listener skip
   evaluation while paused. (Hint: `GoalStore.update` already accepts any
   status.)

5. **Let the evaluator read the full context**
   Remove the `[-24:]` and compare (with a real model) judgment quality vs.
   per-evaluation token cost.

---

## Next chapter

From s01 to s17, every mechanism stands on its own:

```
Agent Loop → Tool → Registry → Permission → Event Log → Turn/Step
→ Prompt Assembly → Skills → Subagent → Compaction → Tasks → Background Jobs
→ Event Bus → Plugins → Capability → Team → Goal
```

The last step: **assemble them into one complete machine**, and verify with a
real task —

> "Help me find out why the tests are failing and fix them."

The harness never learns that this is a debugging task.
It just lays out tools / context / state / permission / session —
and watches the model walk the whole way by itself.

→ [s18 — Full Harness](../s18_full_harness/)
