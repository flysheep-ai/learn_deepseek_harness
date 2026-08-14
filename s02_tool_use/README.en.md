# s02 — Tool Use

**中文版：[README.md](README.md)**

[s01](../s01_agent_loop/) → **s02** → [s03](../s03_tool_registry/) → … → s18

> How does a model gain the **ability to act**?
>
> The answer is not inside the model. It's in the harness.

---

## The problem the last chapter left

In s01 the model can *say* `ls -la`, but:

- It **cannot execute** the command
- The command's output never enters `messages`, so the model never sees
  feedback from the world
- The person "running the command and pasting the result back" is you

In s01, one user input = one model call. The model gets exactly **one** chance to
speak and must guess the right answer with zero visibility into the environment.
It is guessing blind.

---

## What this chapter solves

Replace the middleman with code. And when you do, one thing happens: **the loop
grows a second layer.**

```
Outer loop (already in s01):   user input → … → user input → …
Inner loop (new in s02):       model call → tool → model call → tool → model call
                               ↑ within one user input, the model may act many times
```

This inner loop is what people usually mean by **Agent Loop**.

---

## The new core concepts

### 1. Tool Schema — the action space the model can see

```python
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command in the working directory.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]
```

This is not documentation — it is a **contract**:

- If you don't list it, the model doesn't know it exists
- If you list it, you must actually be able to execute it

The model's action space = the list you hand it. Not one word more.

### 2. tool_call → execution → tool_result fed back

```python
messages.append({
    "role": "tool",
    "tool_call_id": call.id,     # ← must be returned verbatim
    "content": output,
})
```

This is the single most important `append` in the whole harness.

> The tool ran in the real world, but the model will **not automatically know
> what happened**. The harness must translate the environment's observation into
> a message the next model request can see — otherwise the step never happened.

`tool_call_id` is the only pairing between call and result. Break the pairing and
the model-side API errors out — in s10, the hardest part of context compaction is
**not breaking this pairing**.

### 3. The loop's continuation condition

```python
if not reply.wants_tools:
    return reply.text        # the model asked for no tools → it considers the task done
```

Look carefully at the weight of this line:

> **Whether to continue is the model's output, not a harness `if`.**

The harness never **knows** whether this is a "read files" task or a "run tests"
task. It knows one thing: the model still wants a tool, so spin once more.

This is the first iron law of the course:

> **Model decides. Harness enables.**

None of the next 16 chapters will violate it. Come back to this line when you
reach multi-agent cooperation in s16 and the goal loop in s17.

---

## Minimal architecture diagram

```
   user input
     │
     ▼
  ┌────────────────────── agent_loop ──────────────────────┐
  │                                                        │
  │   messages ──▶ LLM ──▶ Reply                           │
  │      ▲                   │                             │
  │      │                   ├── no tool_calls ──▶ return text │
  │      │                   │                             │
  │      │                   └── has tool_calls            │
  │      │                          │                      │
  │      │                          ▼                      │
  │      │                     run_bash()                  │
  │      │                          │                      │
  │      └──── role:"tool" message ◀┘                      │
  │            (observation fed back)                      │
  └────────────────────────────────────────────────────────┘
```

---

## Run it

```sh
python s02_tool_use/code.py --demo
```

Output:

```
You > what's in this directory? what does hello.py contain?
  $ ls -1
  hello.py
  notes.txt
  $ cat hello.py
  print("hello harness")
Model > The directory has hello.py and notes.txt. hello.py is one print line.
```

**Stare at that role sequence.** One user input produced 3 model calls and 2
tool executions. s01 could only ever be `user → assistant`.

---

## Execution flow

```
messages = [user]
  ↓
① chat(messages, tools=TOOLS)
   → Reply(tool_calls=[bash("ls -1")])
   → messages += [assistant(tool_calls=…)]
   → run_bash("ls -1") → "hello.py\nnotes.txt"
   → messages += [tool(result)]
  ↓
② chat(messages, tools=TOOLS)                 ← now the model can SEE the output
   → Reply(tool_calls=[bash("cat hello.py")])
   → messages += [assistant, tool]
  ↓
③ chat(messages, tools=TOOLS)
   → Reply(text="The directory has…", tool_calls=())    ← no more tools
   → exit the loop, return the text
```

---

## Why it's designed this way

### Why tool failures don't raise exceptions

```python
except OSError as e:
    return f"Error: {e}"          # ← return a string, don't raise
```

**A tool failure is normal business, not a crash.**

The model can see `command not found` and simply try a different command — that
autonomy is exactly what we want. But if the exception escapes `agent_loop`, the
whole session dies.

This rule reappears throughout:

> **A tool's failure must become an observation the model can read.**

Likewise, when the model hallucinates a nonexistent tool name, we return
`Error: no tool named xxx` instead of a `KeyError`. Hallucinated tool names are
the norm, not a bug.

### Why stderr is returned together with stdout

Throw stderr away and the model believes the command succeeded, then keeps
working from a wrong world model.

> **Observations must be honest.**

The harness may limit what the model can do (s04), but it must never deceive the
model about what happened.

### Why there is a MAX_STEPS

```python
for step in range(1, MAX_STEPS + 1):
```

The model can get stuck in tools (re-reading the same file, retrying the same
failing command).

Note what this limit is: **resource protection, not intelligence.** We do not
write "if 3 consecutive failures, switch strategy" — that would be the harness
thinking for the model. We only say "you may spend at most this many steps";
how to spend them is the model's business.

s17 formalizes this kind of constraint as the Goal's budget.

---

## What changed vs. the previous chapter

| | s01 | s02 |
|---|---|---|
| Model calls per input | exactly 1 | 1 to N, **N decided by the model** |
| Tools | none | `bash` |
| Roles in messages | user / assistant | user / assistant / **tool** |
| Loop layers | 1 (conversation) | 2 (conversation + **step**) |
| Can the model see the environment | no | **yes** (via tool_result feedback) |

The concrete code diff:

```python
# s01
reply = provider.chat(messages, system=SYSTEM)
messages.append(reply.as_assistant_message())

# s02
reply = provider.chat(messages, tools=TOOLS, system=system)   # ← tools now passed
messages.append(reply.as_assistant_message())
if not reply.wants_tools:                                     # ← new stop condition
    return reply.text
for call in reply.tool_calls:                                 # ← execute + feed back
    output = run_bash(call.arguments["command"], cwd)
    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
```

---

## What real systems do on top

- **Parallel tool execution**: the model can request several tool_calls at once.
  We execute serially. DeepSeek Harness classifies calls with
  `isConcurrencySafe`, pools the safe ones, and barriers the rest. Real, but it
  would drown the pre/execute/post shape itself — so this course stays serial.
- **Timeouts and cancellation**: real systems give each tool a `timeoutMs` and
  thread an abort signal into the tool body. We only have a 60s hard timeout on
  the subprocess.
- **Truncation strategy**: we bluntly slice `[:20000]`. Real systems keep head
  and tail and note "N lines omitted in the middle", because the model often
  needs the error message at the end.

---

## Try it yourself

1. **Add a `get_current_time` tool**
   Add one schema to `TOOLS` and one `elif` in the dispatch. Then count: how many
   places did you touch? (schema once, dispatch once)
   Remember this number — s03 is aimed straight at it.

2. **Throw stderr away**
   Change `(r.stdout + r.stderr)` to `r.stdout`, then ask the model to
   `cat a file that doesn't exist`. Watch it get fooled into believing the file
   is empty.

3. **Delete the tool-result feedback**
   Comment out `messages.append({"role": "tool", ...})`.
   (The model-side API errors out because a tool_call has no paired result.
   The error itself demonstrates how mandatory the pairing is.)

4. **See what the model actually received**
   In the demo, add `print(provider.seen[1]["messages"])` and look at the
   context of request 2.

---

## Next chapter

Do "Try it yourself" #1, then imagine adding four more tools: `read` / `write` /
`edit` / `glob`.

```python
if call.name == "bash":
    ...
elif call.name == "read":
    ...
elif call.name == "write":
    ...
```

This `elif` chain has three problems, and they get worse as it grows:

1. Schemas live in `TOOLS`, implementations live in the `elif` — the two will
   **drift apart** (rename a parameter on one side, forget the other)
2. How are arguments passed? `run_read(**call.arguments)`? If the model omits a
   required parameter, it's a `TypeError`
3. Want a uniform "pre-execution check" for all tools? Write it once per branch

Once tools multiply, what structure does a harness actually need?

→ [s03 — Tool Registry](../s03_tool_registry/)
