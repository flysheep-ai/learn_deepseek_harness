# s03 — Tool Registry

**中文版：[README.md](README.md)**

[s02](../s02_tool_use/) → **s03** → [s04](../s04_permission/) → … → s18

> **A Tool is the action space the harness offers the model.**
>
> This chapter turns that sentence into a concrete object.

---

## The problem the last chapter left

s02 has one tool, so this works fine:

```python
if call.name == "bash":
    output = run_bash(call.arguments["command"], cwd)
```

Now grow to six tools and three problems appear immediately:

### Problem 1: schema and implementation drift apart

```python
TOOLS = [{"name": "edit", "parameters": {..."old_text"...}}]   # at the top of the file
...
elif call.name == "edit":
    run_edit(path, old, new)                                   # 200 lines away
```

One day you rename the parameter from `old_text` to `old`, fix the
implementation, forget the schema. The model sends arguments per the old schema
— `TypeError`, session dead.
**Two places defining one thing are guaranteed to drift.**

### Problem 2: the model's arguments are untrusted

The model will omit required parameters, send extra ones, and hallucinate
nonexistent tool names. All of it is **the norm**:

```python
run_edit(**{"path": "a.py"})              # TypeError: missing old_text
run_edit(**{"path": "a.py", "foo": 1})    # TypeError: unexpected foo
handler = TOOL_HANDLERS["teleport"]        # KeyError
```

Every one of these kills `agent_loop`.

### Problem 3: shared logic gets copy-pasted N times

Want a "pre-execution check" for all tools? Write it once per branch. Add a
sandbox tomorrow? Write it six more times.

---

## What this chapter solves

Four objects, not one more:

```
Tool          one capability: schema + implementation, bound together
ToolResult    execution result: content (for the model) + is_error (for the harness)
ToolRegistry  the single source of the action space: add, get by name, emit schemas
ToolExecutor  turns one tool_call into one ToolResult; shared logic written once
```

---

## The new core concepts

### 1. Tool: schema and implementation bound together

```python
@registry.tool(
    "edit", "Replace one exact text span in a file.",
    {"type": "object",
     "properties": {"path": {...}, "old_text": {...}, "new_text": {...}},
     "required": ["path", "old_text", "new_text"]},
)
def run_edit(path: str, old_text: str, new_text: str) -> str:
    ...
```

Schema and function sit **next to each other in the source**. When you rename a
parameter, both sides are in front of your eyes. Problem 1 goes from "requires
discipline" to "structurally impossible".

### 2. ToolRegistry: the single source of the model's action space

```python
provider.chat(messages, tools=registry.schemas(), system=system)
```

From now on, "the tools the model can see" and "the tools that can actually
execute" are **the same set**.

This single-source property becomes critical in s09: a subagent restricted to
`read` / `grep` is restricted via `registry.restrict()`. The consistency of
"filtered tools appear neither in the prompt nor in execution" is only
guaranteed because the registry is the one and only source.

### 3. ToolResult: two audiences, two fields

```python
@dataclass(frozen=True)
class ToolResult:
    content: str              # for the model
    is_error: bool = False    # for the harness
```

Why not just return a `str`? Because the harness itself needs to know success
from failure: s04's permission denials, s13's failure-rate stats, and s18's
retry decisions all read `is_error`, while the model only needs `content`.

### 4. ToolExecutor: the mount point for shared logic

```python
def execute(self, name, arguments) -> ToolResult:
    tool = self.registry.get(name)
    if tool is None:      return ToolResult(f"Error: no tool named '{name}'. Available: …", is_error=True)
    missing = [...]
    if missing:           return ToolResult(f"Error: {name} is missing required params: …", is_error=True)
    cleaned = {k: v for k, v in arguments.items() if k in known}   # drop extras
    try:
        return ToolResult(tool.handler(**cleaned))
    except Exception as e:
        return ToolResult(f"Error: {type(e).__name__}: {e}", is_error=True)
```

All three kinds of model-side errors become **strings the model can read**; no
exception escapes.

Note how the error messages are written:

```
Error: no tool named 'teleport'. Available: bash, read, write, edit, glob, grep
Error: edit is missing required params: old_text, new_text
Error: text not found in hello.py. Read the file first to confirm its content.
```

Each one tells the model **what to do next**. Just saying "failed" costs the
model an extra step of guessing.

> **Failure messages to the model must be actionable.**

---

## Minimal architecture diagram

```
                 ┌─────────────┐
   registration Tool ─▶│ToolRegistry │
                 └──────┬──────┘
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
      schemas()               get(name)
            │                       │
            ▼                       ▼
      ┌──────────┐          ┌──────────────┐
      │   LLM    │─tool_call▶│ ToolExecutor │─▶ ToolResult
      └──────────┘          └──────────────┘
                                    │
                             unknown tool / missing params /
                             extra params / exception safety net
```

---

## Run it

```sh
python s03_tool_registry/code.py --demo
```

The demo has the model walk a real workflow (glob → read → grep → edit → bash to
verify), then **deliberately** hit three error paths:

```
→ teleport  to='mars'
  ✗ Error: no tool named 'teleport'. Available: bash, read, write, edit, glob, grep
→ edit      path='hello.py'
  ✗ Error: edit is missing required params: old_text, new_text
→ read      path='../../../etc/passwd'
  ✗ Error: ValueError: path escapes the workspace
```

None of the three crashes the session.

---

## Why it's designed this way

### Why agent_loop was not rewritten

The diff between s02 and s03 is exactly two places:

```python
# s02
reply = provider.chat(messages, tools=TOOLS, system=system)
if call.name == "bash":
    output = run_bash(call.arguments["command"], cwd)

# s03
reply = provider.chat(messages, tools=executor.registry.schemas(), system=system)
result = executor.execute(call.name, call.arguments)
```

That fact is itself a conclusion:

> **A good harness abstraction does not change the shape of the loop.**
> If adding a mechanism forces you to rewrite `agent_loop`, the mount point is
> probably wrong.

s13 turns this sentence into a hard constraint ("don't keep modifying the Agent
Loop"). From s03 to s18 the skeleton of `agent_loop` barely moves — what grows
is everything around it.

### Why `safe_path` exists this early

```python
def safe_path(p: str) -> Path:
    root = WORKSPACE.resolve()
    path = (root / p).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"path escapes the workspace: {p}")
    return path
```

The model may request any path; **whether it may touch it is the harness's
call.**

This is the first appearance of "the harness limits the model" in code. s04
formalizes it into a permission model; s15 turns it into a swappable filesystem
provider (swap in a `MemoryFileSystem` and the whole tool set runs in memory).

### Why ToolExecutor looks thin right now

It does three things today, and it is indeed thin. But it is a **place**:

```
s03  execute() = validate + call
s04  execute() = pre → validate → execute → post          ← permission moves into pre
s13  execute() = dispatch events, let plugins hang on     ← pre/post become events
```

**Have the place first, then hang things on it.** This is the other side of
"abstraction is triggered by pain": the pain has appeared (shared logic copied
six times), so building the place now is justified; but s13's need for events is
no reason to import an EventBus in s03.

### Why `schema()` doesn't include the handler

```python
def schema(self):
    return {"name": ..., "description": ..., "parameters": ...}   # no handler
```

The registry holds **far more** than the model may see.

Real harnesses guard this boundary with an explicit allowlist — timeout budgets,
concurrency flags, and UI renderers all live on the tool definition and none of
them may leak into a model request. Here we simply don't include them, but you
should know this boundary is **deliberately maintained**, not accidental.

---

## What changed vs. the previous chapter

| | s02 | s03 |
|---|---|---|
| Tools | 1 | 6 (bash / read / write / edit / glob / grep) |
| Schema & implementation | separate places | **bound on `Tool`** |
| Dispatch | `if/elif` | `registry.get(name)` |
| Model sends bad arguments | `TypeError`, session dies | a readable error result |
| Hallucinated tool name | one "unknown tool" line | lists available tools, model self-corrects |
| Shared logic | copied per branch | `ToolExecutor`, once |
| Result type | `str` | `ToolResult(content, is_error)` |
| `agent_loop` | — | **2 lines changed** |

---

## What real systems do on top

- **Full JSON-Schema validation**: we only check required-present and drop
  extras; no type checking. Real systems validate or coerce types (`limit: "10"`
  rejected or cast).
- **Structured tool output contracts**: DeepSeek Harness requires every tool to
  declare an `output.schema`; execution results are validated into canonical
  JSON, then `render()`ed into model-facing text. One value serves both the
  model and the UI card. We return strings.
- **Per-tool timeout and concurrency flags**: fields like `timeoutMs` /
  `isConcurrencySafe` belong to the registry and never enter a model request.
- **UI presentation**: `presentCall` / `presentResult` render an invocation as
  "reading main.py" instead of a JSON blob.

---

## Try it yourself

1. **Add a tool and count the touches**
   Add `list_dir`. In s02 that was 2 edits (schema + elif); in s03 it's one
   decorated function.

2. **Register two tools with the same name**
   `ToolRegistry.register` throws `duplicate tool name: read` immediately.
   Think about why this should explode at **startup**, not at runtime.

3. **Delete the validation**
   Comment out the `missing` check and run the demo. Watch
   `edit(path='hello.py')` turn from a friendly error into a `TypeError` that
   kills the loop.

4. **Give the model a broken tool**
   Write a handler that `raise RuntimeError("boom")` and watch it get caught by
   `ToolExecutor` into `Error: RuntimeError: boom` — session continues.

5. **Look at the action space**
   `print(registry.schemas())` — that JSON blob is the model's **entire**
   knowledge of the world. Everything it can do is in there. Not one word more.

---

## Next chapter

The model now has six tools, including:

```python
bash("rm -rf /")
write("~/.ssh/authorized_keys", "...")
```

`safe_path` blocked path escapes for the file tools, but **`bash` can do
anything**.

The Registry solved "how to give the model capabilities" but said nothing about
"whether to".

> A harness doesn't just give the agent abilities — it also **limits** them.

So where should the limits live? Inside `run_bash`? Then every one of the six
tools duplicates it — back to pre-s03 problems. Inside `ToolExecutor.execute()`?
Then how does it know `bash` is dangerous while `read` is safe?

→ [s04 — Permission](../s04_permission/)
