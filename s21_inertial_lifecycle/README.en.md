# s21 — Inertial Lifecycle

**中文版：[README.md](README.md)**

[s20](../s20_reactive_coeffects/) → **s21** → [s22](../s22_session_lifecycle/)

> A lifecycle should not be driven by load/unload commands.
> It should be driven by the **comparison** between "what is" and "what should
> be" — and once a transition starts, it **runs to completion** before
> responding to new targets. That is inertia.

---

## The problem the last chapter left

s20's components have two states and instantaneous transitions. Real systems:
an activation runs many effects (slow, possibly failing); mid-transition,
dependencies change again — stop or finish? A failed transition leaves what
behind? A half-activated component?

## What this chapter solves

A teaching version of the Cordis paper's §4.2/§4.3 lifecycle:

```
INACTIVE → RELOADING → ACTIVE → UNLOADING → INACTIVE
               ↑            │
               └── chain switching ──┘
```

The core is **two views**:

```
target     what it should be: ⊥ = should not run; otherwise the resolution of each declared key
committed  the resolution it actually activated against
```

Every rule is driven by comparing them.

## The new core concepts

### 1. Comparison-driven, not command-driven

`refresh()` recomputes target → compares with committed → starts a transition
on difference. You don't call load/unload; the **fact** that "actual ≠ should"
starts the transition. Commands only change the inputs to target (retire /
provide / unprovide).

### 2. Inertia

While a transition is in flight, refresh only **records** the new target —
never interrupts. When the transition finishes, compare once more: target
changed again → **chain** into the next transition.

### 3. Failure: recover first, record second, no contagion

```
setup fails
  → run the accumulator built so far (back to "nothing installed")
  → record the error on the fiber
  → exit through the same unload path as normal shutdown
```

A failed component contributes zero to the state; the error does **not**
propagate to the parent; siblings keep running — exactly what a plugin host
wants.

### 4. Failed fibers do not re-enter

In the paper, L-Begin's premise is INACTIVE(⊥); a failure result INACTIVE(ξ)
never restarts — retrying against an unchanged environment fails the same way.
Wait for a human.

## Run it

```sh
python s21_inertial_lifecycle/code.py --demo
```

Five acts: comparison-driven → automatic transition → inertia → the failure
path → the lineage table back to s06/s12/s19/s20.

## Why it's designed this way

**Why "should vs actual" beats commands**: imperative (`component.load()`) makes
every caller judge "should it load?"; comparison concentrates the judgment in
one place (refresh) — callers only change facts. **No "forgot to call" or
"called twice".**

**Why failure exits through the same unload path**: a separate error path means
two cleanup logics, and "how far did the cleanup go on failure" becomes
unanswerable. One exit: **every outcome arrives only through L-Unload** — zero
contribution by a failed component is a free corollary.

## What changed vs. s20

| | s20 | s21 |
|---|---|---|
| Component states | idle / active | **four states + in-flight** |
| Transitions | instantaneous sync | **inertial: finish, then respond** |
| Transition failure | unhandled | **recover, record, no contagion, no re-entry** |
| Driver | provide/unprovide mutate status directly | **target vs committed comparison** |
| retire vs remove | absent | **separated**（request first; rules carry it out） |

## What real systems do on top

- **Iterator effects**: real reload splits setup into steps; after each step a
  staleness check compares the target — stale means abort, and the accumulator
  rolls back the partial build (paper §4.3.2).
- **Async transitions + inertia handles**: `fiber.inertia` is the handle of the
  in-flight transition, wired to UI cancel/progress (dsh Algorithm 5).
- **The Confluence theorem**: with independence and totality added, dynamic
  history converges to the static assembly — Theorem 73's engineering meaning.

## Try it yourself

1. Split `_begin_reload`'s setup into three steps; change the target before
   step two; watch the accumulator roll back.
2. Let a failed component retry after its target changes (drop "no re-entry")
   and compare the two behaviors for the "environment is fixed now" scenario.
3. Add s20's three-stage order (guard waiting for dependents) to unload and
   bolt the two chapters together.

## Next chapter

s21's fibers live in memory. What happens after a process restart?

s05 said the log is the only truth — but a recovered session is byte-identical
to a native one. Some judgments need to know "did *I* write this history?"

→ [s22 — Session Lifecycle](../s22_session_lifecycle/)
