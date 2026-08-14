# s12 — Background Jobs

**中文版：[README.md](README.md)**

[s11](../s11_task_system/) → **s12** → [s13](../s13_event_bus/) → … → s18

> Tool calls are inherently **synchronous**: call and wait for the result.
>
> But some work is inherently **asynchronous**.

---

## The problem the last chapter left

The agent needs to run tests:

```python
bash("python3 -m pytest")
```

This project's tests take 5 minutes. For those 5 minutes, the entire Agent
Loop **freezes** — the model waits, the user waits, nothing else can happen.

Meanwhile the model could be reading code and preparing the next edit.

Worse: s03 gave bash a 60-second hard timeout, so the command **never
finishes** — the model gets "timed out" and falls into a "retry" death spiral.

---

## What this chapter solves

A second execution shape for the harness:

```
Synchronous tool call                Asynchronous job
┌──────────────────┐             ┌──────────────────────────────┐
│ bash("pytest")   │             │ bash_background("pytest")     │
│   ⏳ blocks 5 min │             │   → "job bash-1 started" (instant)│
│   ← result        │             │ the model keeps reading, editing…│
└──────────────────┘             │ [job bash-1 done] ← harness injects │
                                 └──────────────────────────────┘
```

Demo measurements:

```
Control: bash("python3 slow_test.py") blocked 1.2s — the whole loop frozen
Turn 1:  0.0s (didn't wait for the test), 6 steps of other work
```

---

## The new core concepts

### 1. Job ≠ Task (the two words are easy to confuse)

```
Task   the model's **intent**    "replace prints with logging"   across turns, persistent, written by the model
Job    one **execution**         "run the pytest command"         has a process, managed by the harness
```

One Task may trigger several Jobs; a Job may relate to no Task at all.
**Two dimensions, not two granularities.**

### 2. The registry owns lifecycle, the producer owns execution

```python
class JobRegistry:
    def start_bash(self, command, cwd, session) -> Job: ...
    def get(self, job_id) -> Job | None: ...
    def running(self) -> list[Job]: ...
    def stop(self, job_id) -> str: ...
    def take_finished_unnotified(self) -> list[Job]: ...
```

Registry: **identity and lifecycle** (id, status, cancel, snapshots).
Producer: **how it runs** (here `subprocess` + a thread).

Adding a new job kind ("run a subagent in the background") needs no Registry
changes. s16 uses this.

### 3. `pump_jobs()` — the real point of the chapter

Running things in the background isn't hard (spawn a thread). The hard part is
**how the result gets back to the model**.

A tool call has a natural return path (`tool_result`). An async job has
**none** — when it finishes, the model may be doing something else, or already
stopped.

So the harness must **push actively**. Push where? The inbox, again:

| Chapter | Source | source |
|---|---|---|
| s06 | user interjection | `steering` |
| s08 | skill body | `skill` |
| s12 | job completion notice | `job` |

The demo shows it directly:

```
#  4 user/message  source=user    run slow_test.py and convert cli.py to logging
# 53 user/message  source=job    [background job bash-1 failed]
```

**Three different sources, one entry point, one claiming mechanism.**

That's the payoff of s06's inbox abstraction — adding a new "thing the harness
wants the model to know" needs no new channel.

### 4. The `notified` flag

```python
def take_finished_unnotified(self) -> list[Job]:
    for j in self._jobs.values():
        if j.status != "running" and not j.notified:
            j.notified = True
            out.append(j)
```

Without it, every step re-injects the same completion notice and floods the
context.

One of the classic traps of poll-and-event hybrid structures.

### 5. `job_output` does not block

```python
if job.status == "running":
    return f"job {job_id} still running ({job.elapsed:.1f}s). Do something else; I'll notify you."
```

`join()` here and the async silently degrades back to sync.

Tell the model honestly "not ready yet" and let it decide whether to wait or
work — the iron law again: **observations honest, decisions to the model.**

---

## Minimal architecture diagram

```
   model: bash_background("pytest")
        │
        ▼
   JobRegistry.start_bash()
        │  ├─▶ session: job/start（log-only）
        │  └─▶ threading.Thread(subprocess.Popen)  ⟳ running in the background
        │
        └─▶ returns "job bash-1 started" immediately    ← tool_result, model continues
                    ⋮
              （the model did 4 other steps; the turn ended naturally）
                    ⋮
   job finishes
        │
        ▼
   pump_jobs()   ← called before every step claims input
        │  ├─▶ session: job/end（log-only）
        │  └─▶ Inbox.put(result, source="job")
        │
        ▼
   claimed by the next step → user/message（SURFACE）→ enters the model's context
```

---

## Run it

```sh
python s12_background_jobs/code.py --demo
python s12_background_jobs/code.py --demo --debug
```

The two turns are the point:

- **Turn 1**: the model starts a background job, then edits cli.py, checks the
  job status, ends. **0.0s total.**
- **Turn 2**: the input is **not user-typed text** — it's the job completion
  notice the harness injected. The model reads the output and marks the task
  `failed` with the reason.

---

## Why it's designed this way

### Why the turn does **not** wait for background jobs at its end

```python
else:
    # Note: a running job does not count as "owed".
    # This turn ends honestly; the notice wakes the next one.
    reason = "natural-stop"
    break
```

Wait for jobs here and async degrades back to sync — just moved from inside
the tool to the end of the turn.

s06 defined a turn as "the drain of one input". A background job is not an
input — it is something that will **produce** an input later. So end this turn
and let the notice wake the next one.

### Why `pump_jobs` runs **before** `claim()`

```python
if prompt_registry is None:
    pump_jobs(session, inbox)
claimed = inbox.claim()
```

A just-finished job is seen by the **current** step, not the next one.
One step's difference = one model call of latency.

### Why `job/start` and `job/end` are log-only

The model sees two things: the `bash_background` return value and the later
injected notice. Showing it the harness's bookkeeping on top is duplication.

But those two events matter to **humans**: replaying the log shows exactly when
the job started, how long it ran, and its exit code.

### The cost of `daemon=True`

```python
threading.Thread(target=runner, daemon=True).start()
```

The main process may exit without waiting for background jobs — the cost is
that **unfinished jobs vanish on exit**.

Real systems drain explicitly at shutdown, or hand jobs to a separate daemon
process. We chose simplicity; you should know the consequence.

---

## What changed vs. the previous chapter

| | s11 | s12 |
|---|---|---|
| Long commands | block the whole loop (and hit the 60s timeout) | **run in the background, return instantly** |
| New objects | — | `Job` / `JobRegistry` |
| New tools | 9 | **13** (+`bash_background` `job_status` `job_output` `job_stop`) |
| New events | — | `job/start` `job/end`（log-only） |
| New section | — | `jobs`（lists only running ones） |
| Path of async results | — | `pump_jobs()` → inbox → `user/message` |
| What drives a turn | user input only | **also job completion notices** |

---

## What real systems do on top

- **Multiple job kinds**: DeepSeek Harness's `JobKindMap` is extensible
  (`bash` / `subagent` / …); the registry treats kinds as opaque id
  namespaces.
- **Ownership and permissions**: who may stop a job? Real systems use owner
  authorization, not "know the id, control the job". We didn't implement it.
- **Streaming output**: a running job should offer "peek at current progress"
  (a `tail`), not just the final dump. We only deliver output at the end.
- **Persistence**: jobs vanish on process restart. Real systems store job state.
- **Scheduled jobs**: cron-style scheduling is the same job mechanism plus a
  trigger.
- **Backpressure**: 50 simultaneous jobs would melt the machine. Real systems
  have concurrency caps and queues.

---

## Try it yourself

1. **Make `job_output` block**
   Add `while job.status == "running": time.sleep(0.1)`. Run the demo and watch
   Turn 1's duration go from 0.0s to 1.2s —
   **one line destroyed the async mechanism.**

2. **Remove the `notified` flag**
   Watch the same notice get injected repeatedly.

3. **Add a job kind**
   Write `start_subagent(preset, task)`: run an s09 subagent in the background.
   (Zero Registry changes — the value of "registry owns lifecycle, producer
   owns execution".)

4. **Test `job_stop`**
   Start `sleep 60`, then `job_stop`. Check `job/end`'s status is `killed`.

5. **Watch the two ways a turn starts**
   ```sh
   python s12_background_jobs/code.py     # real model
   > start "sleep 3 && echo done" in the background
   > （press Enter）      # nothing new → a 0-step turn
   > （press Enter）      # job done → the notice is claimed, the model reacts
   ```

---

## Next chapter

Stop and count what `ToolExecutor` now carries:

```python
def execute(self, call_id, name, arguments, session, turn, step):
    session.append(EV_TOOL_CALL, ...)        # 1. logging
    short = self.pre_execute(ctx, session)   # 2. validation 3. permission 4. approval 5. permission log
    result = ... self.run_body(ctx)          # 6. execution
    result = self.post_execute(ctx, result)  # 7. truncation 8. trace
    session.append(EV_TOOL_RESULT, ...)      # 9. logging
```

Three product requests arrive:

1. "Show me average latency per tool" → edit `ToolExecutor`
2. "Dangerous commands must run in a sandbox" → edit `ToolExecutor`
3. "Redact secrets from tool results" → edit `ToolExecutor`

**Every cross-cutting concern edits the same file.**

And they don't know about each other: does redaction run before or after
truncation? Sandbox before permission? The order questions pile up as `if`s
inside `execute()`.

Worse: the three requests may come from three teams — or be optional features.
CI wants the sandbox, local dev doesn't; enterprise wants redaction, OSS
doesn't.

s04 said "permission is not an `if` in the loop — it's a stage of the
pipeline". But now **the pipeline itself is the new universal edit point**.

Can these concerns attach **from outside** instead of living inside
`ToolExecutor`?

→ [s13 — Event Bus](../s13_event_bus/)
