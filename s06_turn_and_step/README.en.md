# s06 — Turn and Step

**中文版：[README.md](README.md)**

[s05](../s05_session_event_log/) → **s06** → [s07](../s07_prompt_assembly/) → … → s18

> Is one user input equal to one model call?
>
> No. And unless that difference is expressed explicitly, budgets, interrupts,
> and "do something when the round ends" all have nowhere to live.

---

## The problem the last chapter left

s05's log has 19 events, but it is **flat**:

```
# 3 assistant/message   (requested 1 tool)
# 7 tool/result
# 8 assistant/message   (requested 1 tool)
#12 tool/result
```

**Which events belong to the same model call? Which belong to the same user
input?**

The log can't answer. Three needs fall apart:

| Want to do | Why it's impossible |
|---|---|
| "At most 20 steps per user input" as a budget | "step" has no boundary, can't be counted |
| "Auto git-commit when this round ends" | there is no "round ended" moment |
| The user interjects mid-run | does it belong to this round or the next? Undefinable |

The `for _ in range(MAX_STEPS)` from s02–s05 was faking an answer to the first
problem all along — it's just a loop counter, absent from the log, reset on
resume.

---

## What this chapter solves

Two levels of structure, written into the log:

```
Turn  the **drain** of one input. Contains **zero or more** Steps.
Step  one model request + the tool executions it triggered.
```

Plus the supporting mechanism: **Inbox** (input queues up, claimed by a step).

---

## The new core concepts

### 1. Step = one model request + the tool executions it triggered

Note "the executions it triggered" belong to the **same** step, not the next
one. One model reply requests 3 tools — those 3 executions are all part of
**that one** step.

### 2. Turn = the drain of one input

A turn opens before its first input is claimed and closes once nothing is owed.

```python
if reply.wants_tools:
    pass                      # tool results owe the model a request → continue
elif inbox:
    pass                      # the user interjected → continue within the same turn
else:
    reason = "natural-stop"   # nothing owed → done
    break
```

Neither continuation condition inspects **task content**. The harness still
doesn't know whether this is a debugging task or a documentation task.

### 3. A zero-step turn is legal

```
Turn 3
  └── Turn 3 end  reason=no-input steps=0
```

The turn opened, but there was no input to claim / the input was filtered out /
it was cancelled — a turn with no step.

Not an edge case: it's a fact that must be recorded — an attempt happened and
never reached the model. If turn and step were one concept, this record would
have no home.

### 4. Inbox: input queues first, claimed by a step

```python
inbox.put("wait — also check whether main() is still there", source="steering")
```

Before s05, user input became `user/message` **immediately**. Once a turn can
span several steps, the question appears:

> The user interjects while the model is at step 3 — which turn does it belong to?

Answer: it queues in the inbox and is **claimed by the next step**. It belongs
to the current turn — it affects the model's next request immediately, without
waiting for the round to end.

The demo's Turn 2 demonstrates it:

```
Turn 2
  ├── Step 1
  │     user(user)        check config.py
  │     tool call         read
  ├── Step 2
  │     user(steering)    wait — also check whether main() is still there   ← interjected
  │     tool call         grep
  ├── Step 3
  │     model             DEBUG=True, TIMEOUT=30; main() still there
  └── Turn 2 end  reason=natural-stop steps=3
```

**One turn, two user messages, three steps.**

The `source` field distinguishes where input came from (`user` / `steering` /
`injected`). Skill content in s08, subagent results in s09, background-job
completion notices in s12 — all arrive through this same path. Different
sources, one claiming mechanism.

### 5. Tracer: make the inside visible

```sh
python s06_turn_and_step/code.py --demo --debug
```

```
[turn 1 start]
  [step 1]  claimed=1 (user)
    → model request   messages=1 tools=6 system=139chars
    ← model reply     text=0chars tool_calls=1 [read] usage=39/4
    · tool pre        read path='app.py' → allow
    · tool result     read ok 77B
  [step 1 end]
  [step 2]  claimed=0
    → model request   messages=3 tools=6 system=139chars
    ← model reply     text=0chars tool_calls=1 [edit] usage=59/15
    · tool pre        edit path='app.py', … → ask→y
    · tool result     edit ok 10B
  [step 2 end]
  [step 3]  claimed=0
    → model request   messages=5 tools=6 system=139chars
    ← model reply     text=14chars tool_calls=0 [-] usage=61/3
  [step 3 end]
[turn 1 end] reason=natural-stop steps=3
```

Stare at `messages=1 → 3 → 5`: **the context grows by 2 entries per step**
(assistant + tool). That is the source of the problem s10 deals with.

A harness is an abstract system; if you can't see it, you can't learn it. Every
chapter from here on supports `--debug`.

---

## Minimal architecture diagram

```
   user input ──▶ Inbox ─────┐
   injected context ─▶ Inbox ─┤
                          │  claim()
   ┌──────────────────────▼──────────────────────┐
   │  turn/start                                 │
   │    ┌─ step/start ─────────────────────┐     │
   │    │   claim → user/message           │     │
   │    │   derive_messages()              │     │
   │    │   model request                  │     │
   │    │   assistant/message              │     │
   │    │   tool/call → execute → tool/result │  │
   │    └─ step/end ───────────────────────┘     │
   │           │                                 │
   │           ├─ model still wants tools? ──▶ next step │
   │           ├─ inbox has new input?  ──▶ next step    │
   │           └─ neither              ──▶ done          │
   │  turn/end  reason=…                         │
   └─────────────────────────────────────────────┘
```

---

## Run it

```sh
python s06_turn_and_step/code.py --demo
python s06_turn_and_step/code.py --demo --debug
```

The closing statistic is this chapter's summary:

```
3 user messages → 3 turns → 6 steps. The three numbers are all different.
```

---

## Why it's designed this way

### Why the turn number is read from the log, not an in-memory counter

```python
def last_turn(self) -> int:
    return max((e.data["turn"] for e in self._events if e.type == EV_TURN_START), default=0)
```

An in-memory counter is a **second truth**. On resume it resets to zero, the new
events' turn numbers collide with history, and the log is ruined.

Direct corollary of the s05 rule: turn numbers are facts, and facts must be
derivable from facts.

> **Anything computable from the log must not be stored a second time.**

### Why user/message is only written when claimed

s05: input becomes `append(user/message)` immediately.
s06: input queues in the inbox, appended **when a step claims it**.

The difference is the **position** of `user/message` in the log. Now it states
exactly "at which step the model saw this message". Append at enqueue time and
the interjected message would appear before Turn 2 Step 1 — the log would claim
the model saw it in step 1, when it didn't. **The log would lie.**

### Why turn/step events are log-only

Turn / step is the **harness's structure**, not content for the model. The
model cares about the message sequence, not how it's grouped.

The projection skips them, but they turn the log from flat to layered:
`print_turn_tree()` can draw the tree that s05's log couldn't.

### Why MAX_STEPS is per-turn

```python
MAX_STEPS_PER_TURN = 12
```

The `MAX_STEPS` in s02–s05 was an upper bound per `agent_loop` call — in other
words, per user input. We just lacked the vocabulary to say it.

Now we have it: it's the **turn budget**. And s17's Goal introduces the next
layer out: the **round budget** ("this goal may auto-continue at most 5
rounds").

Three budgets map to three structures:

```
round   one outer policy iteration (the goal continued once)
turn    the drain of one input
step    one model request
```

Without this vocabulary, "limit how long the agent runs" is a vague sentence.

---

## What changed vs. the previous chapter

| | s05 | s06 |
|---|---|---|
| Log structure | flat | **layered by turn/step** |
| New events | — | `turn/start` `turn/end` `step/start` `step/end` |
| User input | into the log immediately | **queues in Inbox, claimed by a step** |
| Mid-run interjection | undefinable | enters the **current turn's** next step |
| Step cap | a loop counter | a turn budget, recorded in `turn/end.reason` |
| End reason | lost | `natural-stop` / `max-steps` / `no-input` |
| Visualization | an event list | **turn/step tree** + `--debug` trace |
| Entry function | `agent_loop(...)` | `run_turn(...)` |

---

## What real systems do on top

- **`agent/pre-step` interception**: real harnesses run a waterfall before
  entering a step; listeners can **rewrite** the claimed messages or **reject**
  the batch. A rejection yields a zero-step turn — the industrial version of
  our `reason="no-input"`. s13 implements it with the EventBus.
- **`turn-stopping` checkpoint**: a serial checkpoint before a turn naturally
  ends; s17's goal continuation hangs there ("goal not done? one more round").
- **Cancellation**: real systems thread an abort signal into tool bodies so
  Ctrl-C interrupts immediately and the log records `reason="cancelled"`. We
  only have hard limits.
- **Inbox wake semantics**: some messages wake the driver immediately (user
  questions); others **queue silently** until another message wakes it
  (injected context). Our inbox treats all equally.

---

## Try it yourself

1. **Run a turn into its budget**
   Set `MAX_STEPS_PER_TURN` to 2 and re-run the demo. Watch `turn/end.reason`
   become `max-steps`.

2. **Interject several times**
   Call `inbox.put()` three times in `steer_after_first_read`. Watch them all
   get claimed in the **same** step (`claimed=3`).

3. **Implement a turn-end hook**
   Before `session.append(EV_TURN_END, ...)`, add
   `if reason == "natural-stop": print("[hook] time to git commit")`.
   Note: **this position did not exist in s05** — there was no "round ended"
   moment.

4. **Sum tokens per turn**
   Walk the log and group `request/usage` events by the `turn` field.
   (Precursor data for s10's compaction.)

5. **Count context growth under --debug**
   Watch `messages=1 → 3 → 5 → …`. Work it out: a 20-step turn produces how
   many messages? And if each tool result were 2000 tokens?

---

## Next chapter

Look at this number in the `--debug` output:

```
→ model request   messages=1 tools=6 system=139chars
```

`system=139chars`. It comes from:

```python
def make_system(cwd, reg):
    return (f"You are a coding agent at {cwd}.\n"
            f"Available tools: {', '.join(reg.names())}.\nAct, don't explain.")
```

This function is about to blow up. The coming chapters want to stuff into it:

- the skill catalog (s08)
- the current task list (s11)
- background job status (s12)
- subagent capabilities (s09)
- project conventions (AGENTS.md / CLAUDE.md)

Keep concatenating into that f-string and it becomes a 200-line string nobody
dares touch: adding a skill edits it, adding a tool edits it, adding a task
edits it. And some of that content is **not needed on every request**.

Is the system prompt a constant — or a runtime artifact assembled on every
request?

→ [s07 — Prompt Assembly](../s07_prompt_assembly/)
