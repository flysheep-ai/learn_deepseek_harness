# s05 — Session Event Log

**中文版：[README.md](README.md)**

[s04](../s04_permission/) → **s05** → [s06](../s06_turn_and_step/) → … → s18

> This chapter overturns a hidden assumption of the previous four.
>
> **`messages` is not the truth. It is a projection of the truth.**

---

## The problem the last chapter left

At the end of s04 we have tools, permissions, an audit trail. But everything is
still crammed into one list:

```python
messages: list[dict] = []
executor.audit: list[dict] = []
```

Three problems approach:

**1. The process dies, the session dies with it.** No persistence anywhere.

**2. The audit trail and messages can't be lined up.**
The audit says "call #4 was denied" — but which entry of `messages` is that?
Two lists, two numberings, never aligned.

**3. Some facts must be recorded, never shown to the model.**

| Fact | Into messages? | Dropped? |
|---|---|---|
| The user clicked "deny" at step 3 | pollutes the context | the audit goes blind |
| This request burned 4200 tokens | pure waste | no budgets possible |
| The tool was registered before execution | the model doesn't need it | can't locate crashes |

**These facts have no home.**

And the deeper problem: if `messages` is the truth, then "resume a session",
"replay an execution", "what did the model see at step 3", "fork a new session
from the middle" — **none of these is possible**, because the intermediate
states were overwritten long ago.

---

## What this chapter solves

Move the truth:

```
      ❌ before                        ✅ after

   messages (the truth)            event log (append-only, the truth)
        │                              │
        └─▶ sent to the model          │  derive_messages()
                                       ▼
                                  messages (projection, discarded after use)
```

One sentence:

> **Model-visible means logged.**
> Anything that can reach a model request must be reconstructable from the log.

---

## The new core concepts

### 1. SessionEvent: one immutable fact

```python
@dataclass(frozen=True)
class SessionEvent:
    seq: int          # strictly increasing and contiguous — the event's identity
    type: str         # "user/message" / "tool/call" / ...
    data: dict        # must be pure JSON
    time: float
```

Three properties, all mandatory:

- **Append-only**: once written, never changed. Allow history edits and
  replay/fork/audit all lose their meaning.
- **Sequenced**: `seq` is the identity of an event, and the prerequisite for
  compaction to address ranges precisely (s10 uses it).
- **Serializable**: an event that can't be stored is an event that wasn't
  recorded. So `append()` does a `json.dumps` check on the spot — an
  unserializable event must explode **now**, not when crash recovery finds the
  log already incomplete.

### 2. SURFACE vs log-only: the split to remember from this chapter

```python
SURFACE_EVENTS = {"user/message", "assistant/message", "tool/result"}

# log-only: session/start, tool/call, permission/decision, request/usage
```

Only SURFACE events participate in the projection; the rest stay in the log.

> Model-visible means logged.
> **But the converse is false** — the log deliberately holds plenty the model
> never sees.

This is the confusion most people hit when first reading an industrial harness.
Writing it as two explicit sets keeps it straight.

### 3. Why `tool/call` is log-only, yet still a separate event

The model's tool requests are already inside `assistant/message` (assistant
messages carry `tool_calls`), so projecting them again would duplicate. Hence
not SURFACE.

But it must be **separately recorded, and recorded before execution**:

```python
session.append(EV_TOOL_CALL, {...})   # ← record first
short = self.pre_execute(ctx, session)
result = ... run_body(ctx)            # ← execute (may crash here)
session.append(EV_TOOL_RESULT, {...})
```

A `tool/call` with no paired `tool/result` is hard evidence of "execution
crashed halfway".

Do it the other way (record only after execution) and a crash looks like
**this call never happened** — the most dangerous kind of log: one that lies.

### 4. derive_messages(): a pure projection

```python
def derive_messages(session, upto=None) -> list[dict]:
    messages = []
    for ev in session.events(upto):
        if ev.type not in SURFACE_EVENTS:
            continue
        ...
    return messages
```

The same event stream always yields the same messages; there is no hidden
state.

The `upto` parameter is a free bonus: **the context at any historical moment
can be recomputed exactly.**

---

## Minimal architecture diagram

```
   user input ──┐
              ▼
   ┌──────────────────────────────────────┐
   │        Session（append-only）         │
   │  #1 session/start        log-only    │
   │  #2 user/message         SURFACE     │
   │  #3 assistant/message    SURFACE     │
   │  #4 request/usage        log-only    │
   │  #5 tool/call            log-only    │
   │  #6 permission/decision  log-only    │
   │  #7 tool/result          SURFACE     │
   │  …                                   │
   └───────────┬──────────────────────────┘
               │
               ├──▶ session.jsonl（disk, recoverable）
               │
               ▼  derive_messages()  ← SURFACE only
        ┌──────────────┐
        │   messages   │  discarded after use, recomputed every step
        └──────┬───────┘
               ▼
              LLM
```

---

## Run it

```sh
python s05_session_event_log/code.py --demo
```

The demo has three acts, each proving something the previous four chapters
couldn't do.

### Act 1: 19 events → 8 messages

```
# 1 log-only session/start
# 2 SURFACE  user/message         bump app.py to 0.2.0, then clean up
# 3 SURFACE  assistant/message    (requested 1 tool)
# 4 log-only request/usage        in=41 out=4
# 5 log-only tool/call            read {"path": "app.py"}
# 6 log-only permission/decision  read: allow (read-only)
# 7 SURFACE  tool/result              1  VERSION = "0.1.0"
…
#16 log-only permission/decision  bash: deny (recursive delete of root or home)
#17 SURFACE  tool/result          Permission denied: …
```

**11 log-only events never enter the context** — but they're all on disk.
"the model tried `rm -rf ~` and got blocked" and the denial text the model saw
now sit on **one timeline** (#16 and #17). s04 couldn't do that.

### Act 2: kill the process, recover from disk

```python
del session
restored = Session.load(log_path)     # reads back 19 events
```

Then ask the model "what's the version now? did you change it?" — it answers
correctly, because the recovered context matches the pre-crash context
**event for event**. It wasn't saved; it was **recomputed**.

### Act 3: time travel

```python
derive_messages(restored, upto=5)     # what did the model see at event #5?
→ user / assistant  (only 2 entries)
```

---

## Execution flow

```
user input
   │
   ├─▶ session.append("user/message", …)
   │
   ▼
agent_loop
   │
   ├─ messages = derive_messages(session)      ← re-projected every step
   ├─ reply = provider.chat(messages, …)
   ├─▶ session.append("assistant/message", …)
   ├─▶ session.append("request/usage", …)      ← log-only
   │
   └─ for each tool_call:
        ├─▶ session.append("tool/call", …)     ← before execution, log-only
        ├─▶ session.append("permission/…", …)  ← log-only
        ├─  execute
        └─▶ session.append("tool/result", …)   ← SURFACE
```

---

## Why it's designed this way

### Why re-derive every step instead of caching

```python
for _ in range(MAX_STEPS):
    messages = derive_messages(session)     # recomputed every step
```

Not the most performant choice. But it guarantees one invariant:

> **What the model sees always equals what the log can reconstruct.**

Cache a `messages` beside it for convenience and the invariant quietly breaks —
someone appends to the cache one day, the log never hears about it, and
"behavior diverges after resume" becomes a bug that is nearly impossible to
find.

Real systems do add caches — but as **derived caches with invalidation**, never
a second truth.

### Why agent_loop's parameter changed (the only time in the course)

```python
def agent_loop(provider, messages: list, ...)     # s04
def agent_loop(provider, session: Session, ...)   # s05
```

In s03/s04 I kept saying "the loop didn't change a line". This chapter changed
it, and the change is worth explaining.

The s04 loop **owned the truth**. Therefore:

- Persist? → serialize the loop's internal state
- Replay? → no entry point exists
- "What did the model see at step 3?" → that intermediate state was overwritten

Now the loop only **appends facts**; the context is recomputed before every
request.

> The loop demoted from "state owner" to "fact producer".
> It got simpler, and the system got stronger.

One of the few moments in the course where an abstraction makes code *smaller*.

### Why `Session` has no `messages` field

Deliberate. With that field present, someone will write to it — and then there
are two truths.

**Don't give it the opportunity to exist.**

### Why permission decisions are log-only, not messages

The model doesn't need the permission system's internal shape
(`decision: "ask"`, `approved: false`); it needs the human-readable outcome:
"the user denied this operation; find another way."

**One fact, different representations for different audiences.**
The log stores the full form; the projection gives the model only the part it
needs — which is exactly what "projection" means.

---

## What changed vs. the previous chapter

| | s04 | s05 |
|---|---|---|
| The truth | `messages` list | **the event log** |
| `messages` | stored | **projection, discarded after use** |
| Audit | `executor.audit` (orphan list) | `permission/decision` events, same timeline as messages |
| Token usage | dropped | `request/usage` events |
| After a crash | all lost | `Session.load()` recovers everything |
| Context at a past moment | impossible | `derive_messages(upto=N)` |
| Persistence | none | JSONL, one event per line |
| `agent_loop` parameter | `messages` | `session` |

---

## What real systems do on top

- **`assistant/chunk`**: DeepSeek Harness logs **every streamed chunk** so the
  UI can replay token by token. We're non-streaming; one `assistant/message`
  suffices.
- **`surfaceOp`**: real events can carry an instruction about what to do with
  the surface, e.g. `{op: "replace", start, end}` — compaction replaces a span
  of history with a summary **without deleting any event**. s10 implements a
  simplified version.
- **`session/end-seed`**: marks which events came from a resume/fork seed vs.
  this lifecycle. Without it, a recovered session is byte-identical to a native
  one, and cross-lifecycle pairing checks (like an unclosed compaction lock)
  misfire.
- **Swappable persistence backends**: JSONL / SQLite are each a provider. We
  only have JSONL.
- **Runtime invariants**: real systems assert "anything entering a model
  request is reconstructable from the log" and explode in development. An
  invariant that only survives on discipline will break eventually.

---

## Try it yourself

1. **Hand-write a log, then project it**
   ```python
   s = Session()
   s.append("user/message", {"content": "hi"})
   s.append("assistant/message", {"text": "hi!", "tool_calls": []})
   print(derive_messages(s))
   ```
   That's the whole secret of this chapter: context can be **constructed from
   nothing**.

2. **Add `tool/call` to SURFACE_EVENTS**
   See what happens (the model sees duplicated tool requests). That explains
   why it's log-only.

3. **Add `permission/decision` to SURFACE_EVENTS**
   Implement its projection so the model sees "you were denied because X".
   Then ask: is that information already in `tool/result`? What does duplicating
   it cost?

4. **Replay a real session**
   ```sh
   python s05_session_event_log/code.py           # run a few rounds → session_xxx.jsonl
   python s05_session_event_log/code.py --replay session_xxx.jsonl
   ```

5. **Implement fork**
   Write `Session.fork(source, upto)`: copy the first N events into a new
   session. (A few lines — because the truth is an immutable event stream,
   forking is natural. With a mutable `messages` as the truth you'd need deep
   copies and fear shared references.)

6. **Manufacture a crash site**
   `raise SystemExit` inside `run_body`, run once, then `--replay`.
   You'll see a `tool/call` with no paired `tool/result` — the crash point is
   unmistakable.

---

## Next chapter

The log now has 19 events, but one thing is invisible:

```
# 3 assistant/message   (requested 1 tool)
# 7 tool/result
# 8 assistant/message   (requested 1 tool)
#12 tool/result
```

**Which events belong to the same model call? Which belong to the same user
input?**

The log is flat. One user input may trigger 5 model calls and 12 tool
executions, and they all pile into an undifferentiated stream.

Concrete consequences:

- "At most 20 steps per user input" as a budget → impossible to count; "step"
  has no boundary
- "Do something when this round ends" (auto-commit, goal check) → no such
  moment exists
- The user interjects mid-run — does it belong to this round or the next?

Is one user input really equal to one model call?

→ [s06 — Turn and Step](../s06_turn_and_step/)
