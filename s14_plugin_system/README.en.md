# s14 — Plugin System

**中文版：[README.md](README.md)**

[s13](../s13_event_bus/) → **s14** → [s15](../s15_capability_seams/) → … → s18

> **What does "everything is a plugin" actually solve?**
>
> Not flair. It gives a feature a **boundary**.

---

## The problem the last chapter left

s13 turned cross-cutting logic into listeners, with assembly code centered in
`install_default_listeners()`. But look at the tops of `demo()` and `main()`:

```python
SKILLS = SkillRegistry(...)              # globals ×8
TASKS  = TaskStore(session)
JOBS   = JobRegistry()
PROVIDER_FOR_SUBAGENT = ...
SUMMARIZER = ...
```

**The assembly logic is losing control:**

| Problem | Concrete form |
|---|---|
| No boundaries | "background jobs" = 1 Registry + 4 tools + 1 prompt section + 2 event types + 1 global + 1 pre-step listener — and nothing frames them together |
| Unloading edits N places | Turning jobs off means deleting: tool registrations, a prompt section, a global, assembly code — **four places**, easy to miss the `pump_jobs()` in pre-step |
| Assembly duplicated | `demo()` and `main()` each carry a copy; fix one, forget the other |
| No product shapes | Want a "read-only agent"? No unit exists that can be removed wholesale |

The root cause: **these features have no boundaries.**

---

## What this chapter solves

Collect the scattered assembly into plugins:

```
s13                               s14
┌────────────────────────┐        ┌──────────────────────────────┐
│ SKILLS = SkillRegistry()│        │ harness.use(CoreToolsPlugin())│
│ TASKS  = TaskStore(...) │        │ harness.use(PermissionPlugin())│
│ JOBS   = JobRegistry()  │        │ harness.use(SkillPlugin())    │
│ registry.tool(...) ×13  │        │ harness.use(TaskPlugin())     │
│ prompts.section(...) ×8 │        │ harness.use(JobPlugin())      │
│ bus.on/use(...)     ×9  │        │ …                             │
│ globals             ×8  │        └──────────────────────────────┘
│ demo() and main() copy │         each plugin carries its tools + prompt
└────────────────────────┘         + listeners + service
                                   mounted/unmounted whole, freely composed
```

**The key mechanism is one sentence: registration is a reversible effect.**

---

## The new core concepts

### 1. Plugin: a name + a setup

```python
class Plugin(Protocol):
    name: str
    def setup(self, ctx: "PluginContext") -> None: ...
```

**Deliberately no `teardown` method.**

Because hand-written cleanup always leaks: add a registration in setup, forget
its counterpart in teardown — a bug that is silent, hard to find, and
guaranteed to happen.

Our approach: **the act of registering returns its own disposer**, and
PluginContext collects them automatically:

```python
class ToolRegistry:
    def register(self, tool: Tool) -> Callable[[], None]:
        ...
        return lambda: self._tools.pop(tool.name, None)   # ← reversible
```

```python
class PluginContext:
    def tool(self, name, description, parameters):
        def deco(fn):
            off = self.harness.tools.register(Tool(name, description, parameters, fn))
            self._disposers.append(off)     # ← collected automatically
            return fn
        return deco

    def unload(self):
        for off in reversed(self._disposers):   # reverse order
            off()
```

This is the foreshadowing from s13 (`bus.on/use` returning disposers) coming
to harvest.

### 2. PluginContext: the plugin's world is these five methods

```python
ctx.tool(...)     # register a tool
ctx.section(...)  # register a prompt section
ctx.on(...)       # register an observer
ctx.use(...)      # register a middleware
ctx.provide(key, service)   # provide a service
ctx.require(key)  # consume someone else's service
```

Plugins may **not** touch the harness's internals directly.

Note how `require` fails:

```python
raise RuntimeError(
    f"plugin {self.plugin_name} needs service '{key}', but no plugin provides it."
    f"Available: {', '.join(self.harness.services)}")
```

**Fail early, and say who's missing.** No "auto-sorting dependencies" magic —
explicit load order reads better than implicit derivation.

### 3. Services are fetched by key — not by type, not by import

```python
ctx.provide("timing", stats)       # TimingPlugin provides
ctx.require("timing")              # another plugin consumes
```

"Who implements tasks" is decided at **runtime**. s15 pushes this idea to its
natural conclusion: a formal capability seam.

### 4. Harness: no privileged core

```python
class Harness:
    def __init__(self, session, cwd):
        self.bus = EventBus()
        self.tools = ToolRegistry()
        self.prompts = SystemPromptRegistry()
        ...
```

It has **no features of its own** — bash comes from a plugin, permission from a
plugin, even "log the tool call" comes from `SessionLogPlugin`.

> **There is no privileged core to patch.**
>
> You change behavior without modifying this class.

The concrete evidence sits in `SessionLogPlugin`'s comment:

> Unload it and the agent keeps working, but the log has no tool/call or
> tool/result — and the model goes blind, because derive_messages projects from
> the log. Which proves the point: **the log is not a side effect; it's the
> spine.**

### 5. Profile: one plugin list = one product shape

```python
h = build_harness("full", ...)       # 14 plugins, 13 tools
h = build_harness("minimal", ...)    #  8 plugins,  6 tools
h = build_harness("readonly", ...)   # 10 plugins,  8 tools
```

A new product shape = one branch. **No plugin, run_turn, or ToolExecutor
changes.**

Note the readonly distinction between "not installed" and "installed but
disabled":

> Not installed means the tools aren't in the model's prompt at all — it won't
> think to try.

---

## Minimal architecture diagram

```
                 build_harness(profile, ...)
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
   CoreToolsPlugin  PermissionPlugin    JobPlugin
        │               │                   │
        │  setup(ctx)   │                   │
        └───────┬───────┴───────────────────┘
                ▼
        ┌──────────────┐
        │PluginContext │  tool() / section() / on() / use() / provide()
        │  _disposers  │  ← every registration's disposer collected here
        └──────┬───────┘
               │ unload() = run all disposers in reverse
               ▼
        ┌──────────────┐
        │   Harness    │  bus / tools / prompts / services / inbox / rt
        │ （no privilege）│  ← unloading any plugin never touches it
        └──────────────┘
```

---

## Run it

```sh
python s14_plugin_system/code.py --demo
python s14_plugin_system/code.py --demo --debug
python s14_plugin_system/code.py --profile readonly    # read-only agent, real model
```

Six demo acts:

```
【1】14 plugins contribute 13 tools, 8 prompt sections, 5 services, 11 listeners
【2】plugins cooperate（tasks, redaction, permission all live）
【3】unload("jobs"): tools −4, prompt −1, service −1, listeners −1 — one line
【4】after unload, the model truly can't reach bash_background anymore
【5】three profiles, three product shapes; readonly's write is denied
【6】a 30-line NotePlugin added without touching any existing code
```

---

## Why it's designed this way

### Why tools are no longer registered with decorators on globals

s03's style:

```python
@registry.tool("bash", "…", {…})
def run_bash(command): ...
```

s14's style:

```python
@ctx.tool("bash", "…", {…})
def _bash(command): ...
```

The difference: tools are now **closures** capturing the plugin's own cwd.
In s13, `run_bash` read the global `WORKSPACE`, so it could never serve two
workspaces. Now one process can host two full harnesses, each with its own cwd.

### Why SkillPlugin puts its service, tool, and prompt section in one plugin

Because they are **one feature's** three pieces.

Unload it and all three vanish together. That's the literal meaning of "a
feature has a boundary".

In s13, "disable the skills feature" meant: delete a global + a tool
registration + a prompt section + assembly code — four places, kept straight by
memory. Now it's `h.unload("skills")`.

### Why RuntimeContext no longer has skills/tasks/jobs fields

s07–s12 added one field per feature to RuntimeContext — each feature editing a
shared data structure, the same disease as "each feature edits ToolExecutor".

Now features carry their own state (in their services) and prompt sections read
from services directly. RuntimeContext keeps only what **every** feature needs.

### Is this over-abstraction?

Check (s17 does a fuller audit; this is the chapter-local check):

| Abstraction | Implementations | Worth it |
|---|---|---|
| `Plugin` / `PluginContext` | 13 plugins | yes — demo act 6 shows zero-touch extension |
| `Harness` | 1 | yes — defers "what the system is" to assembly time |
| key-based services | 5 services | yes — s15 keeps building on it |

Same test as always: **what did this abstraction make simpler?**
Concretely: unloading a feature went from "edit 4 places" to "one line";
product shapes went from "none" to "a profile parameter".

---

## What changed vs. the previous chapter

| | s13 | s14 |
|---|---|---|
| Assembly | `install_default_listeners()` + a pile of globals | **plugins carry everything** |
| Unload a feature | edit 4 places, from memory | `h.unload(name)` |
| New feature | attach listeners (cross-cutting only) | **write a plugin**（tools+prompt+listeners+service） |
| Product shapes | none | profiles（full / minimal / readonly） |
| Globals | 8 | **0** |
| Tool definitions | read global `WORKSPACE` | **closures capture their own cwd** |
| `run_turn` | references SKILLS/TASKS/JOBS/SUMMARIZER | **references no feature** |
| Compaction/injection | hardcoded calls inside run_turn | pre-step listeners owned by plugins |

`run_turn` now references no feature at all — unload anything and it stays
untouched.

---

## What real systems do on top

- **Declared dependencies**: Cordis's `inject` declares service requirements
  and the loader **auto-orders** accordingly. We rely on "use in require order"
  plus fail-early on missing. Auto-ordering is stronger, but implicit
  derivation costs readability — a teaching project chooses explicit.
- **A reversible-effect library**: `ctx.effect()` is the core API in Cordis;
  every registration (listeners included) returns a disposer and unloading is a
  runtime guarantee, not a convention. Our `_disposers` is its minimal form.
- **Configuration-driven assembly**: real profiles aren't an elif in code but a
  YAML config with patchable layers. That's a deployment concern, not a
  harness-understanding concern.
- **Per-agent plugin scope**: plugins can mount onto exactly one agent (s09's
  scope concept).
- **Hot reload**: unload + re-setup hot-swaps a plugin, provided its state
  lives in services and disposes cleanly — our structure already supports it;
  the demo just doesn't show it.

---

## Try it yourself

1. **Unload session-log and read the log**
   `h.unload("session-log")`, run a turn, open session.jsonl — no tool/call or
   tool/result anywhere (so the model goes blind). Proves the log is the spine,
   not a side effect.

2. **Write a WelcomerPlugin**
   Hang on `EVT_TURN_START` and print a greeting. Then unload it.

3. **Annotate the readonly profile**
   Add "you are in read-only mode" to readonly's IdentityPlugin text.
   Note: **you edited exactly one line of build_harness.**

4. **Swap two plugins' load order**
   Move `TaskPlugin()` before `CoreToolsPlugin()` — does it break?
   (No: they don't depend on each other. Build an example that does, to feel
   what `require` is for.)

5. **Mount NotePlugin onto readonly**
   Add one `h.use(NotePlugin())` line in the readonly branch. Feel what
   "composition" means.

---

## Next chapter

Open `CoreToolsPlugin` and look at `read` / `write` / `bash` — they call
`pathlib` and `subprocess` directly.

Product request: "let the agent run in a remote sandbox."

- file operations must become RPC calls into the sandbox
- shell execution must become sandboxed processes
- **permission, redaction, audit… all unchanged** (they're the event layer,
  implementation-agnostic)

But look at the code: `read` calls `_safe_path(cwd, path).read_text()`. To swap
implementations you'd edit **every tool handler** — back to the s13 disease,
just relocated from ToolExecutor to the six handlers.

Can "filesystem" and "shell" be made swappable **capabilities**, with the
tools as mere **consumers** that don't know who implements them?

For instance: one harness running on `LocalFileSystem`, another on
`MemoryFileSystem` — the other 1000 lines **identical**?

→ [s15 — Capability Seams](../s15_capability_seams/)
