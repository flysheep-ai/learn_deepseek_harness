# s19 — Revertible Effects

**中文版：[README.md](README.md)**

**s19** → [s20](../s20_reactive_coeffects/) → [s21](../s21_inertial_lifecycle/) → [s22](../s22_session_lifecycle/)

> You have **already used** everything in this chapter, back in s14.
> Here we give it a name, a reason, and the questions it cannot answer.

---

## The problem the last chapter left

s14's core mechanism is one line: registration returns a disposer; unload runs
disposers in reverse. It works, but cannot answer:

- Why is "registration returns a disposer" right, instead of an author-written teardown?
- Why does reverse order suffice? Who guarantees it?
- Why does withdrawing **one** component from a running system sometimes fail?
- What does "recover" mean if `free` cannot restore the heap layout?

The general answers are §3.1 of the Cordis paper (deepseek-harness's foundation):
**revertible effects**.

## What this chapter solves

Turn s14's one line into a small theory:

```
effect = a modification + its inverse      Γ → Γ × (Γ → Γ)
tracking at runtime: the inverse goes onto an accumulator
recovery: run the accumulator in LIFO order
out-of-order withdrawal: needs independence (commutation)
"back to the start": read up to observational equivalence
```

## The new core concepts

### 1. Every atomic effect carries its own one-sided inverse

Three decisions in one signature — `ctx.effect(forward, inverse)`:

- **Supplied where applied**: an inverse is usually valid only at the one state
  it was made for — `close()` can only close *this* fd, so `open()` must return it.
- **One-sided only**: the inverse takes the post-state back to the pre-state.
  `free` doesn't restore the heap layout; "the resource is reclaimed" is enough.
- **Composites invert in reverse order for free** (twisted composition): the
  author writes an inverse per *atomic* effect; composites derive theirs automatically.

### 2. The soundness invariant

```
accumulator(state) == initial state
```

Every `effect()` preserves it. **Complete recovery is a structural guarantee**,
not the author's diligence — exactly what s14's `_disposers` does.

### 3. Independence

For one component, LIFO holds free. With several components' effects
**interleaving**, withdrawing one runs its inverses against states **moved by
others**. Demo part 3 shows the failure: C1's inverse is **perfectly correct**,
yet applied against a state moved by C2, withdrawal lands on 9 instead of 0.

### 4. Recovery means observational equivalence

Physical state cannot be restored bit-for-bit, so "back to the start" reads:
*two states are related ⟺ no observer can tell them apart*. And what an
observer is given is s20's coeffects — **dependencies define the granularity at
which recovery counts**.

## Minimal architecture diagram

```
  effect(f, g)                     recover()
  ┌──────────────┐               ┌──────────────┐
  │ state ← f(state)│            │ while acc:   │
  │ acc ← [g] + acc │            │   acc.pop()()│
  └──────────────┘               └──────────────┘
        │ every application preserves: acc(state) == initial
        ▼
   ┌──────────────────────────────────┐
   │ one component: LIFO holds free    │
   │ interleaved: independence needed  │
   └──────────────────────────────────┘
```

## Run it

```sh
python s19_revertible_effects/code.py --demo
```

Four acts: the mechanism → the s14 comparison → out-of-order withdrawal,
independent vs not → observational equivalence.

## Why it's designed this way

**Why disposers beat author-written teardown**: VSCode's `activate/deactivate`
splits creation and cleanup into two functions — add a registration, forget its
cleanup: silent, hard to find, guaranteed to happen. Returning the disposer
makes the forgetfulness structurally impossible.

**Why reverse order suffices**: composites invert in reverse order
*automatically* — no ordering code to write.

**Why out-of-order withdrawal needs a condition**: the paper's Corollary 21 —
under independence, n inverses in **any permutation** reach the initial state.
Without it, fall back to LIFO or to s20's declared ordering discipline.

## What changed vs. s14

| | s14 | s19 |
|---|---|---|
| Mechanism | `_disposers` + reverse | **same mechanism, now formalized** |
| Names | none | track / recover / accumulator / independence |
| Out-of-order withdrawal | undiscussed | independence condition + a live failure demo |
| Semantics of "recover" | undiscussed | observational equivalence |

## What real systems do on top

- **Witnesses (𝔈*)**: the paper requires each inverse to genuinely revert *at
  the state it was applied*; we trust the author instead of checking at runtime.
- **Iterator effects**: real setup is a multi-step iterator, abortable per step
  with the accumulator rolling back the partial build (s21).
- **Independence is statically checkable**: in Cordis, operations on distinct
  keys are automatically independent (Theorem 40) — the single-source
  discipline of s20 makes that premise hold.

## Try it yourself

1. Add an assertion to `effect()` that randomly checks `acc(state) == initial` —
   does it ever fail?
2. Build two "partially independent" components and observe withdrawal.
3. Fix C2's inverse in demo part 3 to a correct `//10` and compute what
   "correct withdrawal" should have produced.

## Next chapter

When independence fails, the paper's answer is "declare an ordering". And
declarations (coeffects) were only half-used in s14: `require()` checks once at
setup, then nobody notices when a dependency gets unloaded.

Should dependency satisfaction be wired once — or **re-evaluated on every
context change**?

→ [s20 — Reactive Coeffects](../s20_reactive_coeffects/)
