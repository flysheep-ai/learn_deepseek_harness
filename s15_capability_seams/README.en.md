# s15 — Capability Seams

**中文版：[README.md](README.md)**

[s14](../s14_plugin_system/) → **s15** → [s16](../s16_agent_team/) → … → s18

> Why should Filesystem / Shell / LLM be decoupled behind interfaces?
>
> Because **swap one provider and the whole product follows — without touching
> a single tool**.

---

## The problem the last chapter left

Open s14's `CoreToolsPlugin` and look at `read` / `write` / `bash` — they call
`pathlib` and `subprocess` directly.

Product request: "let the agent run in a remote sandbox."

- file operations must become RPC calls into the sandbox
- shell execution must become sandboxed processes
- permission, redaction, audit… all unchanged (event layer,
  implementation-agnostic)

But look at the code:

```python
def _read(path, limit):
    lines = _safe_path(cwd, path).read_text()     # ← implementation hardcoded in the handler
```

Swapping implementations means editing **every tool handler** — the s13
disease again, relocated from ToolExecutor to the six handlers.

---

## What this chapter solves

Turn "filesystem" and "shell" into **seams**:

```
One seam = three roles

Service Definition（interface）      Provider（implementation）    Consumer（user）
┌──────────────────┐         ┌──────────────────┐      ┌──────────────────┐
│ class FileSystem │  ◀─impl─│ LocalFileSystem   │      │ read / write /   │
│   read/write/…   │    emen-│ MemoryFileSystem  │      │ edit / glob /    │
│ class Shell      │    ts──│ LocalShell        │      │ grep / bash      │
│   run(...)       │         │ DryRunShell       │  ─use│ interface only    │
└──────────────────┘         └──────────────────┘      └──────────────────┘
  Consumers never import Providers; providers register by key, decided at runtime
```

The demo runs three worlds:

```
World A  LocalFileSystem + LocalShell    real reads/writes, real execution
World B  MemoryFileSystem + DryRunShell  pure memory, commands only rehearsed
World C  LocalFileSystem + DryRunShell   read local, preview execution
```

**All three worlds run the exact same tool code.**

---

## The new core concepts

### 1. The three roles

**Service Definition** — imports no implementation:

```python
class FileSystem(Protocol):
    def read(self, path: str, limit_lines: int | None = None) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    ...
```

**Providers** — `LocalFileSystem` / `MemoryFileSystem` / `DryRunShell`.

**Consumer** — the six tools in `CoreToolsPlugin`:

```python
def setup(self, ctx) -> None:
    fs = ctx.require("fs")       # gets the interface, never learns who implements it
    shell = ctx.require("shell")

    @ctx.tool("read", ...)
    def _read(path, limit=None):
        return fs.read(path, limit_lines=limit)     # ← interface only
```

> Consumers never import Providers. Local disk, memory, or a remote sandbox —
> none of its business.

Swapping a provider is one parameter:

```python
h = build_harness("minimal", cwd, ..., fs=mem, shell=DryRunShell())
```

### 2. The interface is deliberately small

```python
class FileSystem(Protocol):
    def read(...); def write(...); def edit(...)
    def glob(...); def grep(...); def exists(...)
```

Exactly the six methods the tools need.

**Every method added to the interface is one every provider must implement.**
Add `chmod` and `MemoryFileSystem` plus any future remote provider must follow.
Interface size is the trade-off between capability and implementation cost.

### 3. Boundary checks live in the provider

```python
# LocalFileSystem
def read(self, path, limit_lines=None):
    lines = self._resolve(path).read_text()      # _resolve does the boundary check
```

In s03 `safe_path` sat beside the tool handler; in s14 it was called inside
each tool's closure.

Now it's written **once, in the provider**. And the benefit isn't just less
code:

> Swap in a remote sandbox provider and the boundary semantics become the
> sandbox's own guarantee — the tools need no changes, because they never knew
> what a path looked like.

### 4. The LLM is a seam too

Look back at `harness_llm.py` — it has been one since s01:

```
Definition: LLMProvider Protocol
Providers:  OpenAICompatProvider / AnthropicProvider / ScriptedProvider
Consumer:   run_turn（only calls provider.chat）
```

This project used a seam from the very first chapter — s15 only gives it the
name.

And this seam has paid off repeatedly: `--demo` swaps `ScriptedProvider`,
tests swap fake models, users swap DeepSeek/OpenAI — every one of those is a
"provider swap".

---

## Minimal architecture diagram

```
             build_harness(profile, fs=?, shell=?)
                       │
                       ▼
   CapabilityPlugin  ── installs fs / shell into services
                       │
        ┌──────────────┼──────────────────┐
        ▼              ▼                  ▼
   CoreToolsPlugin  JobPlugin        （future: sandbox, LSP…）
     require("fs")   require("shell")
        │              │
        ▼              ▼
   tool handlers contain interface calls only, no implementation details
```

The sentence from DeepSeek Harness's capability-seams doc is worth quoting in
full:

> Filesystem and subprocess providers share one execution world, so pointing
> them at a remote sandbox moves Bash, PTY, and LSP with them, **with no
> provider forks**.

Filesystem and process execution share one "execution world". Point that world
at a remote sandbox and bash, terminals, and LSP all move together — no
per-capability sandbox adapter needed.

---

## Run it

```sh
python s15_capability_seams/code.py --demo
python s15_capability_seams/code.py --demo --debug
```

Watch World B's output:

```
read(app.py)  →     1  VERSION = "0.9.0"      ← content from memory
write(new.txt) → wrote new.txt (5 bytes)
grep(TODO)    → README.md:3:TODO: add config
bash(rm -rf /) → [dry-run] would run: rm -rf /   ← nothing executed
```

**Zero disk I/O, zero process spawns** — and all six tools work normally.
That's the complete effect of a provider swap.

---

## Why it's designed this way

### Why seam and plugin are two concepts

- **plugin** answers "what features does the system have" (mountable,
  unmountable)
- **seam** answers "who implements one capability" (replaceable, multiple
  providers possible)

One plugin (`CapabilityPlugin`) installs a seam's provider; several plugins can
share one seam (`CoreToolsPlugin` and `JobPlugin` both use shell).

s14's `provide/require` was the seam's embryo; this chapter pushes "fetch
services by key" to its natural conclusion: **definition, implementation, and
consumption fully separated.**

### Why MemoryFileSystem is worth its 70 lines

Two reasons:

1. **Testing.** Nondeterministic tool tests run in memory — instant, side-effect
   free. Daily bread for real harnesses: DeepSeek Harness ships memory/replay
   implementations per provider for exactly this.
2. **Teaching.** It proves "swap provider = swap world" at zero cost.

### Why JobPlugin still uses subprocess directly

It's a **deliberate contrast**:

Real harnesses route job execution through the shell seam too (so background
tasks get sandboxed as well), but leaving one non-seam plugin on the field lets
you see both styles side by side — one swaps worlds with a parameter, the other
must edit its implementation.

### Is this over-abstraction?

| Abstraction | Implementations | Worth it |
|---|---|---|
| `FileSystem` | 2 | yes — the second (Memory) is used the same day |
| `Shell` | 2 | yes — DryRunShell composes "preview mode" |
| `FileEntry` / `ShellResult` | — | yes — pure data, JSON-serializable, transmittable by a remote provider |

Same test: **what did this abstraction make simpler?**
Switching the execution environment went from "edit 6 handlers" to "pass one
parameter".

---

## What changed vs. the previous chapter

| | s14 | s15 |
|---|---|---|
| Beneath the tools | direct pathlib / subprocess | **fs / shell interfaces only** |
| Switching the environment | edit each handler | **pass `fs=` / `shell=`** |
| Boundary checks | `_safe_path` inside each tool | **once, in the provider** |
| Testing file tools | needs real disk | runs on MemoryFileSystem |
| Preview mode | doesn't exist | DryRunShell composes it |
| New objects | — | `FileSystem` `Shell`（Definitions）+ 4 Providers + `CapabilityPlugin` |

---

## What real systems do on top

- **Multiple coexisting providers for one seam**: DeepSeek Harness's subagent
  is a registry of named providers (in-process / fork / ACP / Codex…); the
  model chooses whom to delegate to. Bash has exactly one executor. We only did
  "one seam, one provider".
- **Capability probing**: a sandbox provider may have quotas and network
  limits; consumers need to discover them. Another dimension of interface
  design we didn't touch.
- **Definitions carry events**: real Definitions also carry events like
  `fs/write-intent`, letting policy hang on the capability instead of the
  consumers.
- **Composite execution worlds**: dsh's `filesystem` and `subprocess` providers
  share one execution world — one sandbox switch moves four capabilities. Our
  `fs` / `shell` are two separate parameters; weaker than a "world"
  abstraction, but enough to teach the concept.

---

## Try it yourself

1. **Write a CountingFileSystem**
   Wrap LocalFileSystem and count reads and bytes. Pass it into
   build_harness — metrics arrive with zero tool changes.
   (This is the "decorator provider" pattern; permission, caching, and rate
   limiting all work this way.)

2. **Write a RemoteStubFileSystem**
   Every method raises `NotImplementedError("goes over RPC")`. Mount it, run a
   turn — the rest of the harness runs untouched; only fs-touching tools error.
   **That's the proof the decoupling is real.**

3. **Route JobPlugin through the shell seam**
   Have it `shell = ctx.require("shell")` and call `shell.run` inside
   `start_bash`. (Hint: jobs need async, the seam's `run` is sync — think about
   how that seam should look. That's exactly the problem real systems solve
   with "async on top of a sync seam".)

4. **Give MemoryFileSystem directory listing**
   Make `glob("*")` return directories too. See how the `dirs` set helps.

5. **Replay one session in two worlds**
   Take one session's event stream, pair it once with Local and once with
   Memory, and see where tool results diverge — proving "determinism" is a
   provider property, not a harness property.

---

## Next chapter

s09 had subagents, s14 had plugins, s15 had seams. Now combine them:

> "Find the cause of this bug, write a fix, and have someone else review it."

This task needs **multiple agents cooperating**:

- one investigates (explorer)
- one writes the fix (editor)
- one reviews (reviewer)

The natural move is to hardcode the workflow:

```python
if task_type == "research":    research_agent()
elif task_type == "fix":       coding_agent()
elif task_type == "review":    review_agent()
```

**This is the moment the whole course has been warning about** — the code above
is the harness deciding for the model: whom to create, what to delegate, when
to collect results.

Can the harness provide only spawn / send / receive / status, while the
**cooperation strategy belongs entirely to the model**?

→ [s16 — Agent Team](../s16_agent_team/)
