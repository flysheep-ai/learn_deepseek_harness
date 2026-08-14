# s16 — Agent Team

**中文版：[README.md](README.md)**

[s15](../s15_capability_seams/) → **s16** → [s17](../s17_goal_loop/) → … → s18

> The harness provides the cooperation mechanism, **not the cooperation
> strategy**.
>
> This is the chapter where "thinking for the model" is easiest to slip into.

---

## The problem the last chapter left

s09's subagent is one-shot: `spawn` → run → return text → discarded.

Now the user gives a bigger task:

> "Find the cause of this bug, write a fix, and have someone else review it."

This needs **multiple agents cooperating**. The natural move is to hardcode the
workflow:

```python
if task_type == "research":    research_agent()     # ❌
elif task_type == "fix":       coding_agent()       # ❌
elif task_type == "review":    review_agent()       # ❌
```

**The most dangerous moment of the course** — the code above is the harness
deciding for the model: whom to create, what to delegate, when to collect
results.

---

## What this chapter solves

Split cooperation into two halves, each to its owner:

```
Cooperation organized by the model      Mechanism provided by the harness
┌─────────────────────────┐         ┌─────────────────────────┐
│ "spawn explorer first,  │         │ spawn_agent(role, task) │
│  then editor,           │         │ send_message(agent, msg)│
│  let reviewer check,    │         │ receive()               │
│  merge the conclusions" │         │ list_agents()           │
└─────────────────────────┘         └─────────────────────────┘
  strategy belongs to the model        four verbs, nothing else
```

The demo's model (played by a script) runs the full decision sequence:

```
① spawn explorer（persistent）    ← the model decides: investigate first
② receive                         ← collect (also triggers explorer's lazy run)
③ spawn editor（persistent）
④ send_message(editor, "explorer says … please fix")
⑤ receive                         ← collect editor's output
⑥ spawn reviewer（persistent）
⑦ send_message(reviewer, "editor changed … please check")
⑧ receive → list_agents → wrap up
```

**No line of harness code contains this order.** The model produced it step by
step within that turn.

---

## The new core concepts

### 1. Persistent members (vs s09's one-shot subagents)

```python
class MemberAgent:
    def __init__(self, name, preset, parent, tools, bus, tracer, provider_factory):
        self.inbox = Inbox()          # anyone may send messages at any time
        self.outbox: list[str] = []   # outputs queue for the main agent to collect
        self.status = "idle"          # idle | working | done
```

Four differences:

| | s09 one-shot | s16 member |
|---|---|---|
| Lifetime | spawn → run → discarded | **persistent, repeated conversations** |
| Messages | initial task only | inbox accepts deliveries anytime |
| Results | returned text | **outbox queue, collected via receive** |
| Running | runs at spawn | **lazy: acts only when messaged** |

### 2. Lazy execution: whoever gets a message does the work

```python
def run_once(self) -> None:
    if not self.inbox:
        return                      # no messages, no motion
    ...
    outcome = run_turn(..., session=self.session, inbox=self.inbox)
```

Who works, and when, is decided by "who received a message". No central
scheduler, no workflow engine — a **minimal implementation** of the cooperation
mechanism.

Two triggers: `send_message` (process right after sending) and `receive`
(settle accounts before collecting).

### 3. receive delivers only **new** output

```python
fresh = m.outbox[m.delivered:]
if fresh:
    m.delivered = len(m.outbox)
    parts.append(...)
```

The first implementation re-read the whole outbox every receive — the model
re-read old conclusions on every collection and flooded its own context.

The same trap as s12's `notified` flag, in a new location:
**a delivery cursor is mandatory in any poll-and-event hybrid.**

### 4. Preset roles become a registry

```python
def register_preset(preset: SubagentPreset) -> None:
    SUBAGENT_PRESETS[preset.name] = preset
```

A new role ("security reviewer") = one `register_preset()` call — zero lines of
SubagentPlugin touched. Role **definitions** are the harness's (capability
envelopes); **whom to send where** remains the model's.

---

## Minimal architecture diagram

```
                     Main agent
                      │
        ┌─────────────┼──────────────────┐
        │ spawn_agent │ send_message     │ receive
        ▼             ▼                  │
  ┌──────────┐  ┌──────────┐  ┌──────────┴───────┐
  │explorer  │  │ editor   │  │  reviewer        │
  │ inbox    │  │ inbox    │  │  inbox           │
  │ outbox   │  │ outbox   │  │  outbox          │
  │ (read,   │  │ (read,…  │  │ (read, glob,     │
  │  glob,   │  │  edit,   │  │  grep)           │
  │  grep)   │  │  write,  │  │  ← restricted tools │
  │          │  │  bash)   │  │                  │
  └──────────┘  └──────────┘  └──────────────────┘
   each has its own Session / Inbox / RuntimeContext（s09's isolation, intact）
   each runs run_turn — the same loop code as the main agent
```

---

## Run it

```sh
python s16_agent_team/code.py --demo
python s16_agent_team/code.py --demo --debug
```

Watch the structure in the output:

```
★ team member explorer joined (persistent)
  → receive
    ┌─ agent[explorer] processing 1 message
      → read path='core.py'
    └─ agent[explorer] done steps=2 → outbox 57 chars
    ✓ 【explorer · done】
```

Each member's actions happen in **its own context** (visually boxed); the main
context sees only the conclusion that `receive` returns.

---

## Why it's designed this way

### Why members reuse run_turn

```python
outcome = run_turn(self.provider_factory(), None, self.executor, self.tracer,
                   prompt_registry=self.prompts, rt=self.rt,
                   session=self.session, inbox=self.inbox)
```

A member is **not** a second loop — it's `run_turn` with its own
session/inbox/rt/prompts.

The payoff of s05–s14 pushing all state into parameters:
one loop, five contexts (main agent, s09's one-shot subagent, team members,
s17's goal, everything in s18), zero duplicated loop code.

### Why "cooperation strategy" gets hardcoded so easily — and how to check

Hardcoding wears more costumes than `if task_type`. They all share one shape:

```python
if len(unread_replies) >= 2:                      # ❌ deciding when the model reads messages
    collect_all_results()
for member in members.values():                   # ❌ deciding everyone must work
    member.run()
if reviewer_found_bugs:                           # ❌ deciding the next step
    send_back_to_editor()
```

The check is simple:

> **Remove the model — can this logic still complete the cooperation?**
> Yes → harness mechanism (spawn / send / receive / status).
> No, it exists only to produce decisions for the model → out of bounds.

Demo part 4 lists both sides line by line.

### Why team state is not a new session event

s16 adds **zero new event types**.

A member's existence and outputs are tool results in the main session
(`created team member editor…`); a member's state lives in its own event log
(`turn/start` … `assistant/message`).

Real harnesses add a query service for the agent registry (`ctx.agents`), but
persistence still flows through the session log. **If the state is derivable
or already exists, don't invent a second store.**

### Why one member per role, not unbounded instances

```python
if agent in members:
    return f"Error: role {agent} already has a member"
```

s09's MAX_SUBAGENT_DEPTH was anti-nesting; "one member per role" is a different
simplification: the team stays countable on one hand, which is what makes
model-led cooperation tractable.

Real systems allow arbitrary instances (addressed by id). We kept names as
addresses but didn't open the count — complexity control, not a capability
ceiling.

---

## What changed vs. the previous chapter

| | s09 (then) | s16 |
|---|---|---|
| Lifetime | one-shot | **persistent, repeatable conversations** |
| Message direction | parent → child (initial task only) | **both ways**（send / receive） |
| Result path | returned text | **outbox + delivery cursor** |
| When it runs | at spawn | **lazy（only when messaged）** |
| Presets | hardcoded dict | **registrable** |
| Cooperation | one spawn at a time | **the model orchestrates a whole team** |
| New events | — | zero（tool results + members' own logs） |

---

## What real systems do on top

- **An agent registry**: dsh's `ctx.agents` is a live registry with
  `list_agents` / `send_message` / `interrupt_agent` as its control tools.
  Ours is a dict at `h.services["agents"]`.
- **Multiple providers**: real subagents are a seam (s15's machinery) — the
  same spawn interface can back onto in-process / fork / ACP / other products.
- **Lineage**: `parentSession` / `delegationDepth` carried as **data**, tracked
  across processes. Our `session/start` records `parent`.
- **Interruption**: `interrupt_agent` stops a running member.
- **Persistent sessions**: a member's session can be saved and resumed next
  session. Ours rebuild each demo.

---

## Try it yourself

1. **Add a role**
   ```python
   register_preset(SubagentPreset(
       "tester", "Runs tests and reports results; changes no code.",
       ["read", "bash"],
       "Run tests and report failures. Do not modify any file."))
   ```
   **You changed not one line of SubagentPlugin.**

2. **Revert receive to "return everything"**
   Delete the `delivered` cursor and watch the model re-read old conclusions on
   every collection.

3. **Make members run on every message instead of lazily**
   Remove `if not self.inbox: return` and watch idle members burn model calls.

4. **Audit your own code for overreach**
   Search your demo: any `if "fix" in task`-style branch? If so, convert it to
   "give the information to the model, let it decide".

5. **Write a "team minutes" plugin**
   Hang on `EVT_TURN_END` and snapshot team status into the log.
   Note: it must be an **observer** (emit) — changing nothing.

---

## Next chapter

The agent now has a team and can run long tasks. But:

```python
run_turn(...)          # done
# is the goal complete? — nobody knows, and nobody cares. Turn over = over.
```

A goal like "fix this bug" rarely fits one turn:

- the model edits, tests fail → **another round** is needed
- the model stalls (missing dependency, no permission) → someone should know
  "where it's stuck"
- the user closes the terminal for dinner → the goal should **survive**, resume
  on the next boot

None of this exists today. The goal lives only in the model's context; the
turn's end is the real end; nobody will wake it up.

Can a goal become **persistent harness state**:

- an explicit lifecycle (active / blocked / complete)
- a budget (max auto-continue rounds)
- a checkpoint that survives the terminal closing

And then let the harness ask itself one question:

> **At the end of this turn: is the goal done? Should another round start?**

→ [s17 — Goal Loop](../s17_goal_loop/)
