# s10 — Context Compaction

**中文版：[README.md](README.md)**

[s09](../s09_subagent/) → **s10** → [s11](../s11_task_system/) → … → s18

> What happens when the context overflows?
>
> But the real question is: since s05 said the log is the only truth and
> messages are just a projection — what exactly does compaction compact?

---

## The problem the last chapter left

s09 keeps **new** large content out of the main context. But the main context
itself keeps growing:

```
[step 1]  ctx=8/600 (1%)
[step 2]  ctx=419/600 (69%)
[step 3]  ctx=856/600 (142%)   ← overflow
```

The bluntest fix is truncation:

```python
messages = messages[-6:]
```

**This explodes immediately.** Part 2 of the demo runs it for you:

```
naive_truncate(keep=6)  （6 entries / 108 tokens）
  tool       1  VERSION = "0.1.0"…  ← ORPHAN! its paired tool_call was cut off
  assistant  +1 calls
  tool       edited app.py
  ...
```

The first `tool` entry's `tool_call_id` finds no matching `tool_call` — it
lived in the cut-off `assistant` entry. The model-side API errors out and the
whole request fails.

And truncation has a second problem: **forgetting**. The model no longer knows
what it did 20 steps ago and repeats itself.

---

## What this chapter solves

**Compaction shadows the projection, never the log.**

```
Event log (append-only, nothing deleted)
  #4  tool/result   (auth/middleware.py full text)  ┐
  #6  tool/result   (auth/tokens.py full text)      ├─ shadowed
  #10 tool/result   (api/deps.py full text)         ┘
  ...
  #44 compaction/summary  { shadowed_seqs: [4,6,10,…], summary: "…" }   ← newly appended
  ...
        │
        │ derive_messages()  ← skips shadowed events, inserts the summary at the range start
        ▼
messages：[summary] + recent messages
```

> **Shadow, not delete.**
> Compaction changes how history is *viewed*, not what history *is*.

---

## The new core concepts

### 1. Three events form a bracket

```
compaction/start  →  （one model call to write the summary — the only fallible part）  →  compaction/summary  →  compaction/end
```

Why three events instead of one?

Because the model call in the middle is **the only place that can fail**. A
`start` with no paired `end` is hard evidence of "compaction died halfway".

Same reason s05 records `tool/call` before execution:

> **Record the intent first, the outcome second — so a crash site never lies.**

### 2. Safe boundaries: cut only at `step/end`

```python
def find_safe_boundary(session, keep_tokens):
    cuts = [e.seq for e in events if e.type == EV_STEP_END]
```

The safety requirement, in one sentence:

> Within the shadowed range, every `tool_call` must be shadowed together with
> its `tool_result`.

How to guarantee it? **Cut only at `step/end`.**

A step is "one model request + the tool executions it triggered", so at a
step's end, every tool_call of that step already has its paired result. Cutting
at `step/end` keeps pairings complete by construction — no id-counting needed.

**This is s06's "mere grouping" structure paying off for the first time: it
gives the log a set of naturally safe cut points.**

Without s06 you'd write a "scan all tool_call ids and find pairing boundaries"
function — and maintain it for every new event type.

### 3. Re-compaction must **absorb** the previous summary

The first implementation produced this:

```
user  [summary of the previous 3 messages]…
user  [summary of the previous 2 messages]…
user  [summary of the previous 2 messages]…
```

Three summaries stacked side by side, messier each round.

The fix: each `compaction/summary` carries a `supersedes` field listing the
summaries it absorbed; `collect_shadows` skips superseded ones:

```
3 compactions happened, but only the last summary is effective:
  effective #44, shadows 7 messages, absorbed 2 old summaries
  absorbed #22 (still in the log, just no longer projected)
  absorbed #33 (still in the log, just no longer projected)
```

And the summarizer's input includes **the previous summary**:

```python
body = (f"[Previous compaction's summary]\n{carry}\n\n" if carry else "") + _render_for_summary(old_msgs)
```

So established facts propagate from generation to generation instead of
eroding one compaction at a time.

### 4. What the summary must retain

```python
SUMMARIZE_SYSTEM = (
    "Condense this history into a short handover note. Must retain:\n"
    "  1. the user's original goal\n"
    "  2. established facts (file names, line numbers, conclusions)\n"
    "  3. changes already made\n"
    "  4. what remains undone\n"
    "Do not restate raw tool output.")
```

A summary is not "tool output abbreviated" — it's a **handover note**.

Imagine handing the work to another person: you'd tell them the goal, what you
found, what you changed, what's left — not recite the files you read.

---

## Minimal architecture diagram

```
   before every step
        │
        ▼
   estimate_tokens(derive_messages(session)) > limit × 0.75 ?
        │ yes
        ▼
   ┌─────────────────── compact() ───────────────────┐
   │ find_safe_boundary()  ← step/end seqs only       │
   │ fresh = pre-boundary SURFACE events not yet shadowed │
   │ worth it? estimate_tokens(fresh) ≥ limit×0.15    │
   │        │ yes                                     │
   │ compaction/start                                 │
   │   summarizer（input = previous summary + fresh）← may fail │
   │ compaction/summary { shadowed_seqs, supersedes } │
   │ compaction/end                                   │
   └──────────────────────┬──────────────────────────┘
                          ▼
   derive_messages()：skip shadowed, insert summary at range start
```

---

## Run it

```sh
python s10_context_compaction/code.py --demo
python s10_context_compaction/code.py --demo --debug
```

Under `--debug` you see pressure and compaction lining up:

```
→ model request   messages=3  ctx=419/600 (69%)
· compaction     3 messages shadowed  856 → 488 tokens
→ model request   messages=3  ctx=488/600 (81%)
· compaction     5 messages shadowed  795 → 344 tokens
→ model request   messages=3  ctx=344/600 (57%)
```

The demo shrinks the window to 600 tokens (real windows are 128k/200k) so you
see the effect within a few steps.

---

## Why it's designed this way

### Why compact at 75%, not after overflow

Overflow is a **failed request**. By the time it fails you've wasted one call
(money + latency) and must handle a complicated retry path.

Compacting early at 75% costs one cheap summarization call instead.

(Real systems keep both paths: proactive compaction on pre-step pressure, and
reactive compaction on a context-overflow error. The second is insurance, not
the main path.)

### Why "worth it" is measured in tokens, not message count

My first version said:

```python
if len(to_shadow) < 4:
    return False        # ❌
```

Wrong. Four `read` results and four `"edited x.py"` differ by two orders of
magnitude.

Compaction costs a model call, so the threshold belongs on **benefit**:

```python
saving = estimate_tokens(_project_range(session, fresh))
if saving < CONTEXT_LIMIT_TOKENS * 0.15:
    return False        # ✅
```

(This bug genuinely appeared while writing the demo: the first compaction ran,
the next two were skipped for being "too few messages", and the context
climbed to 945/600. Switching to a benefit criterion fixed it.)

### Why the summary is inserted at the range **start**, not the end

```python
if ev.seq in anchors:                    # anchors[min(shadowed_seqs)]
    messages.append({"role": "user", "content": anchors[ev.seq]})
```

The summary is about "what happened before this point" — so it belongs at that
point, keeping the timeline ordered.

At the end, the model would see "recent messages … then a summary about
earlier things". A scrambled timeline.

### Why subagents don't compact

```python
if SUMMARIZER is not None and prompt_registry is None:
```

Subagents are short-lived (s09's explorer runs 4 steps). One extra model call
for compaction isn't worth it.

**Optimizations respect lifespans.** The same mechanism is essential for the
main agent and waste for a subagent.

### Does time travel still work after compaction?

Yes — the most direct dividend of shadow-not-delete:

```python
derive_messages(session, upto=23)     # still the pre-compaction context
```

Because `collect_shadows(session, upto)` is bounded by `upto` too — at event
#23, that `compaction/summary` (#44) didn't exist yet.

**If compaction deleted old messages, this section wouldn't exist.**

---

## What changed vs. the previous chapter

| | s09 | s10 |
|---|---|---|
| Context overflow | request fails | **proactive compaction** |
| Compaction method | — | shadow the projection; the log never loses an event |
| Cut safety | — | only at `step/end`; pairings complete by construction |
| Re-compaction | — | later summaries **absorb** earlier ones (`supersedes`) |
| New events | — | `compaction/start` `/summary` `/end`（log-only） |
| `derive_messages` | SURFACE filter only | **+ skip shadowed, insert summary** |
| Trigger point | — | before every step, 75% threshold |
| Time travel | yes | **still yes** |

---

## What real systems do on top

- **`surfaceOp`**: DeepSeek Harness's summary isn't a special event type — it's
  an ordinary `user/message` carrying `surfaceOp: {op: "replace", start, end}`.
  The projection layer then understands **one operation** instead of growing a
  new special case per compaction strategy. Our `shadowed_seqs` is the
  simplified version.
- **Prune before summarize**: real systems first do **model-free** pruning
  (replace old tool_results with "(omitted)"). If that suffices, the
  summarization call is never spent.
- **The compaction lock**: `compaction/start` is a lock recorded in the log.
  An unclosed start blocks later entries — but must distinguish "left over from
  a previous lifecycle" from "currently in progress". That's one reason dsh
  has the `session/end-seed` event.
- **Real tokenizers**: we estimate `len//4` and get it wrong. Underestimate and
  you compact for nothing; overestimate and you still overflow.
- **Other triggers**: manual `/compact` (we have it), on session resume, on an
  overflow error reply.

---

## Try it yourself

1. **Break the safety boundary**
   Change `find_safe_boundary`'s candidates from `EV_STEP_END` to
   `EV_TOOL_CALL`, run the demo, and watch `← ORPHAN!` appear in the compacted
   projection. The fastest way to understand why cuts belong on step/end.

2. **Disable summary absorption**
   Delete the `supersedes` line and watch three summaries stack up.

3. **Change the summary prompt**
   Set `SUMMARIZE_SYSTEM` to "summarize in one sentence" and (with a real
   model) watch the agent start repeating work it already did.
   **Whatever the summary drops, the model forgets.**

4. **Compact manually**
   ```sh
   python s10_context_compaction/code.py     # real model
   > /ctx        # current context
   > /compact    # compact once
   > /ctx        # again
   ```

5. **Verify time travel**
   ```python
   before = derive_messages(session, upto=20)
   after  = derive_messages(session)
   # before has no summary; after does
   ```

6. **Measure the information loss**
   Before and after compaction, ask the model the same question ("which files
   did you read?") and compare the answers.

---

## Next chapter

Now give the agent a big task:

> "Replace all prints with logging in this project, add type annotations, then
> run the tests to confirm nothing broke."

The model says: "Sure, three steps: ① replace prints ② add annotations ③ run
tests."

Then it starts on step one. Twenty steps later, compaction ran twice.

**It forgot step 3.**

Because "I plan to do three things" only exists in a piece of text the model
said. It is context — it gets shadowed, diluted, drowned across rounds.

The summary prompt says "retain what remains undone" — but that's **praying**
the model writes it right every time, not **guaranteeing** it.

Can a plan live not in the model's head, but in the harness's hands?

→ [s11 — Task System](../s11_task_system/)
