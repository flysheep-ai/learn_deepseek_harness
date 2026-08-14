# s20 — Reactive Coeffects

**中文版：[README.md](README.md)**

[s19](../s19_revertible_effects/) → **s20** → [s21](../s21_inertial_lifecycle/) → [s22](../s22_session_lifecycle/)

> Should dependencies be wired once at initialization — or **re-evaluated on
> every context change**?
>
> deepseek-harness's answer is the latter, plus a complete three-stage
> unloading discipline.

---

## The problem the last chapter left

s14's `require()` answers only "what if the dependency is missing at setup"
(throw). It says nothing about runtime: who tells a dependent when its provider
is unloaded? Does the dependent revive when the dependency reappears? And when
unloading a provider, **who stops first** — otherwise dependents read a
"half-withdrawn" dependency.

## What this chapter solves

**1. Dependency satisfaction is re-evaluated at runtime.** Every
provide/unprovide classifies the change against each component's **declaration**:

```
activating    σ ⊭ d and σ′ ⊧ d   just satisfied → activate
deactivating  σ ⊧ d and σ′ ⊭ d   just withdrawn → deactivate
neutral       otherwise          none of this component's business
```

**2. The three-stage cascade** (Cordis paper §4.3.1):

```
① stop providing   the provider withdraws its keys (dependents recompute now)
② guard            the provider waits for every dependent (transitive) to stop
③ withdraw inverses  only now does the provider run its own inverses
```

## The new core concepts

### 1. Single-source discipline

One key, at most one provider. Without it, "who provides db" is unanswerable —
and the target/committed comparison (s21) plus the cascade order have nothing
to stand on.

### 2. Classification is against the **declaration**, not "did this key change"

A component cares only whether its dependency set as a whole flipped between
satisfied and unsatisfied. Unrelated components are never touched —
O(dependents) propagation, not O(all components).

### 3. Stop providing ≠ unload

`unprovide` withdraws the key; the component's effects stay. Dependents see
"unsatisfiable" first and start their own teardown — the engineering form of
the paper's L-Leave (marking UNLOADING before any inverse runs).

### 4. The guard: dependents' inverses before the provider's

The demo's event stream shows the order directly: app and cache's inverses all
run before db's. A dependent never reads a half-withdrawn dependency — the
engineering form of the paper's Theorem 63.

## Run it

```sh
python s20_reactive_coeffects/code.py --demo
```

Five acts: declarations & single-source → reactive activation → enforcing
single-source → the cascade → dependency returns, components revive.

## Why it's designed this way

**Why "wait for the dependency" instead of "fail at startup"**: s14's
`require()` only expresses static dependencies. In a real ecosystem (Koishi's
4000+ plugins), a plugin and its dependency are usually written by **different
authors** sharing nothing but the key. A temporarily absent dependency is
normal: wait quietly, don't kill the boot.

**Why stop-provide and withdraw-inverses are separate**: if the provider's
inverse runs first (closing the db connection), dependents can no longer reach
it during their own teardown. Hence: let the world know "I no longer provide"
first; once every dependent has landed safely, withdraw your own resources.

## What changed vs. s14

| | s14 | s20 |
|---|---|---|
| When dependency is checked | once, at setup | **every context change** |
| Missing dependency | raises | waits quietly; activates on arrival |
| Dependency withdrawn | nobody notices | **cascade stop** |
| Unload order | no concept | **stop-provide → guard → withdraw** |
| Single-source | duplicate provide raises | named and justified |

## What real systems do on top

- **Realms**: `ctx.isolate(key, realm)` resolves the same key to different
  providers per scope. Our keys are global.
- **Interception**: context-carried metadata, right-biased merge — an outer
  scope constrains how a component uses a dependency **without modifying it**
  (e.g. read-only db grants).
- **Async transitions**: the real cascade is asynchronous (the guard awaits).
  Our teaching version is synchronous recursion — the order is identical.

## Try it yourself

1. Build a three-level chain (a → b → c); unload a; verify c stops first and a
   withdraws last.
2. Rewrite the classifier to compare "did this key change" instead of the
   declaration, and watch unrelated components misfire.
3. Implement a minimal `intercept`: the provider merges context metadata at
   read time; demonstrate a read-only grant.

## Next chapter

s20's components have two states and instantaneous transitions. Real
activations run many effects; deactivations run many inverses — and the target
may change **mid-transition**.

deepseek-harness's answer: **inertia**.

→ [s21 — Inertial Lifecycle](../s21_inertial_lifecycle/)
