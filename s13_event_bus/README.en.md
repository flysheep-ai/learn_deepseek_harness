# s13 — Event Bus

**中文版：[README.md](README.md)**

[s12](../s12_background_jobs/) → **s13** → [s14](../s14_plugin_system/) → … → s18

> This chapter adds **zero new features**.
>
> It moves things around — but after the move, every cross-cutting concern
> accumulated in the first 12 chapters can be added or removed without touching
> the core.

This is the gate into Part 3: **why industrial harnesses need Event / Plugin /
Capability / Isolation.**

---

## The problem the last chapter left

Count what s12's `ToolExecutor.execute()` carries:

```python
def execute(self, call_id, name, arguments, session, turn, step):
    session.append(EV_TOOL_CALL, ...)        # 1. logging
    short = self.pre_execute(ctx, session)   # 2. validation 3. permission 4. approval 5. permission log 6. trace
    result = ... self.run_body(ctx)          # 7. execution
    result = self.post_execute(ctx, result)  # 8. truncation 9. trace
    session.append(EV_TOOL_RESULT, ...)      # 10. logging
```

60 lines, 9 responsibilities. Now three requests:

1. "Average latency per tool" → edit `ToolExecutor`
2. "Dangerous commands in a sandbox" → edit `ToolExecutor`
3. "Redact secrets from results" → edit `ToolExecutor`

**Every cross-cutting concern edits the same file.**

And they don't know about each other: redaction before or after truncation?
Sandbox before permission? The order questions pile up as `if`s — and the
order is **implicit**, decided by whose code happens to sit first.

Worse: the requests may come from three teams, or be optional features — CI
wants the sandbox, local dev doesn't; enterprise wants redaction, OSS doesn't.

s04 said "permission is not an `if` in the loop — it's a stage of the
pipeline". But now **the pipeline itself became the new universal edit point.**

---

## What this chapter solves

```
        s12                              s13
┌────────────────────┐          ┌────────────────────────────┐
│ log tool/call       │          │ bus.emit("tool/call")       │
│ validate params     │          │ bus.waterfall("pre")        │
│ check permission    │          │ bus.waterfall("execute",    │
│ ask human           │          │      terminal=run_body)     │
│ log permission      │   ───▶   │ bus.waterfall("post")       │
│ execute             │          │ bus.emit("tool/result")     │
│ truncate            │          └────────────────────────────┘
│ trace               │
│ log tool/result     │          permission / logging / truncation /
└────────────────────┘          timing / redaction all become
  60 lines / 9 jobs              listeners attached from outside
                                 15 lines / 1 job
                                 **and it never changes again**
```

---

## The new core concepts

### 1. Two dispatch modes — neither is optional

```python
bus.on(event, fn, order)    # emit      —— may see, may not change the flow
bus.use(event, fn, order)   # waterfall —— receives next, may wrap or short-circuit
```

**emit (observe)**: audit, trace, metrics.

```python
bus.on(EVT_TOOL_RESULT, lambda ctx: metrics.record(ctx.name))
```

**waterfall (middleware)**: permission, sandbox, timeout, retry, timing.

```python
def timing(ctx, next_):
    t0 = time.perf_counter()
    next_()                       # calling = delegating to the next layer
    record(time.perf_counter() - t0)
```

Call `next()` to pass through; don't call it to **short-circuit** (everything
below never runs).

**Why must the two be separated?**

Because "may it change the flow" is the listener's **contract**:

- If every listener could short-circuit, any third-party plugin could silently
  neuter permission
- If none could, permission couldn't exist

> **Write the power into the type, not into a convention.**

The separation has a direct consequence:

```python
def emit(self, event, *args):
    for _, _, fn in ...:
        try:
            fn(*args)
        except Exception as e:
            print(f"[bus] observer {event} raised: …")   # swallowed, main flow unaffected
```

An observer's error must **not** affect the main flow — it never had the power
to change it. A middleware is different: it can short-circuit, so its exception
should propagate. Demo part 6 verifies: attach `lambda ctx: 1/0` as an
observer and the tool still returns normally.

### 2. The waterfall implementation is 6 lines

```python
def waterfall(self, event, ctx, terminal=lambda: None):
    chain = sorted(self._middleware.get(event, []), key=lambda e: e[0])

    def step(i):
        if i >= len(chain):
            terminal()
            return
        chain[i][2](ctx, lambda: step(i + 1))

    step(0)
```

Wrap "the next layer" in a closure and let the current layer decide whether to
call it. That's the whole mechanism.

The tool body is the **terminal** of the `tool/execute` waterfall:

```python
self.bus.waterfall(EVT_TOOL_EXECUTE, ctx, terminal=lambda: self.run_body(ctx))
```

### 3. `order` makes sequence explicit

```
waterfall tool/pre-execute   10:validate → 20:permission → 90:trace
waterfall tool/post-execute  10:redact   → 20:truncate
```

`redact` (10) sits **outside** `truncate` (20), so **redact first, then
truncate**.

Reversed, the truncated-away half of a secret never gets redacted — and if
someone later changes the truncation policy, the secret leaks.

In s12 this order was implicit (whoever's code came first). Now it's a number
that can be documented, reviewed, overridden.

### 4. Registration returns a disposer

```python
off = bus.on(EVT_TOOL_RESULT, audit)
...
off()      # gone for good
```

Not a nicety: **s14's plugin unload is built entirely on this.**

A registration without a matching withdrawal means the plugin system can only
mount, never unmount.

### 5. The EventBus is not just for tools

`agent/pre-step` is a waterfall; a listener can rewrite or reject the step's
input:

```python
def block_step(pre: StepPreCtx, next_):
    if any("dangerous" in it.content for it in pre.items):
        pre.rejected = True
        return                       # short-circuit
    next_()
```

```
Turn 2: steps=0 reason=rejected
The model was never called.
```

**The zero-step turn from s06 — which only "no input" could produce — now has
a real mechanism.**

Note `StepPreCtx`'s fields:

```python
@dataclass
class StepPreCtx:
    turn: int
    step: int
    items: list[InboxItem]
    rejected: bool = False
    reject_reason: str = ""
```

It can't reach the `session` (no history edits) or the `registry` (no tool-set
changes). **The power boundary is written in the dataclass's fields.**

---

## Minimal architecture diagram

```
   ToolExecutor.execute()
        │
        ├─ emit      tool/call         ──▶ [session-log]
        │
        ├─ waterfall tool/pre-execute  ──▶ [validate] → [permission] → [trace]
        │                                      │            │
        │                            short-circuit = deny; tool body never runs
        │
        ├─ waterfall tool/execute      ──▶ [timing] ──▶ ((run_body))  ← terminal
        │
        ├─ waterfall tool/post-execute ──▶ [redact] → [truncate]
        │
        └─ emit      tool/result       ──▶ [session-log] → [trace]

   run_turn()
        └─ waterfall agent/pre-step    ──▶ [guard]  ← can reject the whole step
```

---

## Run it

```sh
python s13_event_bus/code.py --demo
python s13_event_bus/code.py --demo --debug
```

Seven demo parts, each proving "the core needs no change":

```
【3】redaction
  On disk:   API_KEY=sk-abcdef0123456789
  Model saw: API_KEY=***

【4】timing（a request s12 couldn't do）
  bash   1 call   avg  12.92 ms
  read   1 call   avg   0.08 ms

【5】attach a listener at runtime, then detach it
  attached: ['glob:ok']
  detached: ['glob:ok']   （no new entries — really gone）

【6】an observer raises, the tool still returns
【7】pre-step rejects an entire step  →  steps=0 reason=rejected
```

---

## Why it's designed this way

### Why subagents share the same bus

```python
child_executor = ToolExecutor(child_registry, BUS)     # the same BUS
```

Permission, redaction, audit apply to subagents equally.

Give the child its own bus and those policies **silently disappear** — the
"looks fine but lost a protection layer" class of bug that's nearly impossible
to spot.

(Real systems are finer-grained: subagents have their own scope and may
register **extra** listeners while inheriting global ones. We used the simplest
form.)

### What is `install_default_listeners`?

```python
def install_default_listeners(bus, policy, approver, tracer, timing_stats):
    bus.on(EVT_TOOL_CALL, listener_log_tool_call, order=10, owner="session-log")
    bus.use(EVT_TOOL_PRE, make_validate_listener(), order=10, owner="validate")
    ...
```

It is a **plugin** — just unnamed and un-unloadable.

s14 gives it a name, a lifecycle, an unload path.
**That's "abstraction triggered by pain": first see what it looks like, then
give it a type.**

### Is this over-abstraction?

Worth checking:

| Abstraction | Implementations | Worth it |
|---|---|---|
| `EventBus` | 1 | yes — its value isn't polymorphism, it's **decoupling registerers from callers** |
| `emit` vs `waterfall` | 2 modes, both used | yes |
| listeners | 8, freely addable/removable | yes |

The counterexample would be "five interface layers for one implementation".
We have no `IEventBus`, no `EventBusFactory`, no `AbstractListener` — that's
what over-abstraction looks like.

The test isn't "did you introduce an abstraction" but "**what did this
abstraction make simpler**". Here the answer is concrete: ToolExecutor went
from 60 lines to 15, and never changes again.

---

## What changed vs. the previous chapter

| | s12 | s13 |
|---|---|---|
| `ToolExecutor` | 60 lines / 9 jobs | **15 lines / 1 job** |
| Permission | an Executor method | `tool/pre-execute` listener |
| Logging | appends inside the Executor | `tool/call` `tool/result` listeners |
| Truncation | an Executor method | `tool/post-execute` listener |
| Trace | the Executor holds a Tracer | listeners |
| Timing | **impossible** | `tool/execute` listener (8 lines) |
| Redaction | **impossible** | `tool/post-execute` listener (10 lines) |
| Order | implicit (code position) | **explicit (order numbers)** |
| Add/remove | edit code | register / disposer |
| Reject a whole step | only via "no input" | `agent/pre-step` short-circuit |
| New features | — | **zero** |

---

## What real systems do on top

- **Four dispatch modes**: Cordis has `emit` / `waterfall` / `parallel` /
  `serial` — observe / wrap / fan out / ordered-with-return. We took two.
- **Monotonic guards**: after the pre waterfall, a layer that can only **deny
  or abstain**, never allow. Third-party plugins then can't un-deny what
  someone else denied — composable permission needs monotonicity. In our
  waterfall, a small-order listener could theoretically swallow a denial.
- **Typed event contracts**: real systems give each event a precise type via
  declaration merging and generate a who-produces/who-consumes catalog. We use
  string constants + dataclasses.
- **Scope filtering**: an event can dispatch only to one agent's listeners.
- **Exception semantics**: when a middleware throws, real systems normalize it
  into an `isError` result instead of killing the turn. Our middleware
  exceptions propagate.

---

## Try it yourself

1. **Write a timeout middleware**
   ```python
   def timeout(ctx, next_):
       # real timeouts need signals/threads; start by flagging "suspected timeout"
       ...
   bus.use(EVT_TOOL_EXECUTE, timeout, order=5)     # outside timing
   ```

2. **Turn permission off**
   Delete the permission line in `install_default_listeners`.
   Note: **you commented out zero lines of ToolExecutor.**

3. **Swap redact and truncate orders**
   Set redact to 30, craft a long output with a secret at the end, and watch it
   leak.

4. **Add a tool-failure-rate observer**
   ```python
   bus.on(EVT_TOOL_RESULT, lambda ctx: fails.setdefault(ctx.name, []).append(
       bool(ctx.result and ctx.result.is_error)))
   ```

5. **Verify emit cannot change the flow**
   Try `bus.on(EVT_TOOL_RESULT, lambda ctx: setattr(ctx, "result", None))`.
   (It can mutate ctx, but the executor already took the result — think about
   whether that counts as a bug to fix.)

6. **Implement "read-only mode" the right way**
   The wrong way: reject inputs that *mention* writing (that's judging the
   model). The right way: a `tool/pre-execute` middleware that DENYs all write
   tools. Feel the difference between the two mount points.

---

## Next chapter

Open `install_default_listeners`:

```python
def install_default_listeners(bus, policy, approver, tracer, timing_stats):
    bus.on(EVT_TOOL_CALL, listener_log_tool_call, ...)
    bus.use(EVT_TOOL_PRE, make_validate_listener(), ...)
    bus.use(EVT_TOOL_PRE, make_permission_listener(policy, approver), ...)
    ...
```

And then the tops of `demo()` and `main()`:

```python
SKILLS = SkillRegistry(...)
TASKS  = TaskStore(session)
JOBS   = JobRegistry()
PROVIDER_FOR_SUBAGENT = ...
SUMMARIZER = ...
```

**The assembly logic is losing control.**

- A pile of module-level globals (`SKILLS` / `TASKS` / `JOBS` / `INBOX` / `RT` / `BUS`…)
- `demo()` and `main()` each carry a copy of the same assembly code
- Turn off "background jobs"? Delete tool registrations, a prompt section, a
  global, assembly code — **four places**
- Give someone a "read-only agent"? No unit exists that can be removed wholesale

The root cause: **these features have no boundaries.**

"Background jobs" = 1 Registry + 4 tools + 1 prompt section + 2 event types +
1 global — and nothing in the code frames them together.

Can one feature become one **unit** — mounted whole, unmounted whole?

→ [s14 — Plugin System](../s14_plugin_system/)
