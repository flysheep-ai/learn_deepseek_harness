# s23 — Self-Extending Harness

**中文版：[README.md](README.md)**

[s22](../s22_session_lifecycle/) → **s23**（advanced track, continued）

> The model no longer operates only on the filesystem — it operates on **its
> own runtime**. This is the direction the Cordis paper's conclusion names:
> "self-evolving agent harnesses, where an AI agent generates and replaces its
> own harness components continuously".

---

## The problem the last chapter left

s22 completed the session lifecycle. But look back at s14: plugins are written
by **humans** and assembled at **startup**. The model can only call the tools a
human gave it — it can never modify the machine it lives in.

deepseek-harness ships this as a product feature (agent note
2026-07-08-self-referential-cordis-toolset): three tools that let the model
**inspect and modify its own runtime**.

## What this chapter solves

Three tools + three correctness problems:

```
harness_inspect()    observe: which plugins/tools/services am I made of?
harness_mount(code)  extend: write code, mount it as a plugin into myself — live immediately
harness_unmount(id)  withdraw: remove it, back to before the mount
```

The three correctness problems the dsh note names (mattering more than the raw
"let the model run code" mechanic):

1. **Validate at the mount site** — a malformed registration must explode when
   mounted, not when a later request assembles it into a prompt. Demo act 3:
   `required` naming a nonexistent property is rejected at mount, with an
   actionable error.
2. **API visibility** — model-written code calls services whose source it has
   never seen. inspect provides names and signatures, saving many steps of
   blind guessing.
3. **Full disposability** — everything the model mounts must be removable:
   on-demand unmount by the model + cleanup when the host plugin unloads,
   or a long session accretes orphaned listeners.

## The new core concepts

### 1. One mount primitive, not a pile of structured tools

The tempting alternative is `register_tool(name, description, parameters,
code)` plus `register_listener` plus `register_service`… dsh rejected it:

| Dimension | Structured per-capability tools | One mount primitive |
|---|---|---|
| Coverage | tools only; each new capability needs a new tool — an API that grows without bound | one vocabulary (a plugin) covers every capability, present and future |
| Cross-mount composition | inexpressible | native provide/inject (s20's machinery reused directly) |
| Inspectability | registered things don't show as plugins | what you mount is exactly what inspect renders |

### 2. Temporary plugins are not durable

Process memory only: no files written, no config changed, no resume.
**Model-visible ⟺ logged still holds**: mount/unmount are tool/call +
tool/result pairs, and the changed tool set is recorded by the request/header
snapshot. No new event type was invented.

### 3. This is not a sandbox — it's a correctness boundary

The controlled namespace narrows the **API surface visible** to mount code,
not its **authority**. Mounted code can register a short-circuiting listener
that stops the agent's own tool dispatch — same trust level as bash. A real
sandbox is a different problem (s15's seam is its home).

## Run it

```sh
python s23_self_extending/code.py --demo
```

Six acts: inspect → mount a new tool and use it immediately → a bad schema
rejected on the spot → cross-mount provide/inject cooperation → unmount to
zero residue → the trust-boundary note.

## Why it's designed this way

**Why self-extension conflicts with nothing in the first 22 chapters**: mounted
plugins go through s14's PluginContext (registration returns its inverse =
s19's revertible effects), dependencies go through s20's provide/inject,
unload goes through s21's lifecycle. Self-extension is not a new mechanism —
it is **handing the controls of the existing mechanisms to the model**.

**Why temporary plugins must be fully disposable**: the model's exploration is
trial-and-error. A failed mount must not leave half a tool; a successful
experiment must be removable when done. That is s19's revertible effects
cashing out in the self-extension scenario.

**Why trust level = bash**: giving the model code-writing ability while
denying it bash's trust is "handing it the key but welding the door shut".
dsh's choice: an opt-in development tool that consciously chooses trust rather
than pretending security.

## What changed vs. s22

| | s22 | s23 |
|---|---|---|
| Who modifies the runtime | humans (on fork/resume) | **the model (mid-session)** |
| New tools | — | **+3**: inspect / mount / unmount |
| Where validation happens | at event write | **at the mount site** |
| New objects | — | `SelfExtensionPlugin` / `_eval_plugin` |
| New events | — | zero（tool/call pairs + request/header suffice） |

## What real systems do on top

- **Generated API catalog**: dsh's inspect serves from a generated catalog
  (AST scan + freshness checks) rather than a hand-written table — hand tables
  drift the moment a signature changes. Our teaching version reflects the
  runtime directly.
- **vm realm + whitelist façade**: the real mount runs in node:vm; traps steer
  code onto cordis services and close the unguarded-context escape.
- **Dual-realm instanceof**: objects from both host and vm sides are
  recognized correctly.
- **Canonical tool-output contract**: mounted tools' schemas/outputs are
  rebuilt and validated on the host side, so models cannot bypass
  `ToolRuntime.execute`.
- **The waterfall short-circuit warning**: tool descriptions warn the model
  directly that a mounted listener returning without `next()` stops its own
  tool dispatch.

## Try it yourself

1. Mount an **event-listener** plugin (no tool) hanging on EVT_TOOL_CALL to
   count invocations; unmount and verify it's gone.
2. Mount a short-circuiting listener (never calls `next()`) and watch the
   agent's tool dispatch stall — then understand the trust-level warning.
3. Make mount's duplicate-name error actionable ("rename and retry with …").
4. Persist the mounted dict into session events and observe how "temporary
   plugins don't survive resume" breaks.

## End of the advanced track (updated)

s19–s23: revertible effects, reactive dependencies, the inertial lifecycle,
the session seed boundary, and **self-extension**.

> The harness's value is building an operable world.
> deepseek-harness goes further: that world is **revertible** (s19),
> **reactive** (s20), **inertial** (s21), **recoverable** (s22),
> and **the model can modify it with its own hands** (s23).
