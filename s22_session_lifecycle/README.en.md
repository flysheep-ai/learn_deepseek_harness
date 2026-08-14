# s22 — Session Lifecycle

**中文版：[README.md](README.md)**

[s21](../s21_inertial_lifecycle/) → **s22**（end of the advanced track）

> Four questions s05 left unanswered about resume, fork, and restart.
> This chapter fits the last pieces of dsh's session layer.

---

## The problem the last chapter left

s05 built the event log; s17 built the goal. But after a restart:

1. A recovered session is byte-identical to a native one — how to tell "left
   over from a previous lifecycle" from "produced by this one"?
2. Fork was only a "try it yourself" exercise in s05, never implemented.
3. s17 says an active goal auto-continues — but autonomous continuation with
   nobody watching after a resume is a security risk.
4. Re-deriving every step is slow; you want a cache — how, without breaking
   "the log is the only truth"?

## What this chapter solves

Four pieces, all from dsh's docs:

| Problem | Mechanism | Source |
|---|---|---|
| Seed boundary | the `session/end-seed` event | dsh session.md |
| Forking | fork = copy events + mark | completed in this course |
| Auto-continue permission | activation: armed / disarmed | dsh goal.md |
| Caching | derived cache + invalidation | completed in this course |

## The new core concepts

### 1. session/end-seed: the seed boundary

A log-only event marking "everything before me is seed (from resume/fork);
this lifecycle produced none of it". **Why it exists**: a recovered session is
byte-identical to a native one. The demo's case: an unclosed
`compaction/start` (the compaction lock) —

- inside the seed → **stale evidence** from a previous lifecycle's crash: ignore
- in the live region → **an in-progress lock**: must block new entries

Without end-seed, a recovered orphan lock is misread as in-progress — blocking
all new compactions. Deadlock.

### 2. Fork: copy + mark

Because the truth is an **immutable event stream**, forking is a few free
lines. Were the truth a mutable messages list, you'd deep-copy and fear shared
references.

### 3. Activation: separating durable state from process permission

s17 conflated two things into status. dsh splits them:

```
durable phase    active / paused / blocked / complete
                 — durable state, in the log, survives restart
activation       armed / disarmed
                 — process-local permission, reset on restart
```

"The goal isn't done" is a durable fact; "may auto-continue with nobody
watching" is a **security decision**. After resume, automation must not start
itself — someone re-arms first.

### 4. Derived cache: invalidation, not a second truth

A second truth lets someone write to it directly; a derived cache **can only be
refilled by recomputation**. The former breaks eventually; the latter cannot.

## Run it

```sh
python s22_session_lifecycle/code.py --demo
```

Six acts: the crash site → the misjudgment demo → marking fixes it → fork →
activation → derived cache.

## Why it's designed this way

**Why the seed boundary is an event, not an in-memory mark**: an in-memory mark
is lost on the next resume. end-seed is a **fact in the log** — permanent,
replayable, and it participates in fork's copy.

**Why fork must mark**: the child's seed history contains everything pairing
checks need (possible orphan locks, goal states). Unmarked, the child cannot
tell "inherited" from "mine".

**Why activation deliberately does not go into the log**: it is process-local
permission. Persisting it would mean auto-continue resumes automatically after
recovery — exactly the risk to prevent. The security property "resume requires
re-authorization" is guaranteed by **not persisting**.

## What changed vs. s21

| | s21 | s22 |
|---|---|---|
| Lifecycle scope | in-process fibers | **cross-process sessions** |
| New event | — | `session/end-seed` |
| Fork | — | implemented |
| Goal continuation | active ⇒ continue | **activation layer** |
| Derived data | recompute every time | **invalidating cache** |

## What real systems do on top

- **firstLiveSeq**: in dsh, the last `session/end-seed`'s position is exactly
  where `Session.firstLiveSeq` comes from — our `first_live_seq()` is the same
  name and meaning.
- **Resume request headers**: on resume, a `reason=resume` request/header
  snapshot is recorded — s07's mechanism applied at the session boundary.
- **Checkpoint policy**: "force a durability flush every N steps" is a separate
  plugin (dsh-session-checkpoint-policy). Ours appends to disk directly.

## Try it yourself

1. Make fork's seed also copy an end-seed (if the parent has one) and check
   whether `first_live_seq` semantics still hold.
2. Implement the full "disarmed on resume + human re-arms" flow, wired onto
   s17's goal loop.
3. Give DerivedCache per-key version numbers instead of whole-table
   invalidation; compare the two strategies' complexity.

## End of the advanced track

s19–s22 fill in what makes deepseek-harness **different from other projects**:
revertible effects, reactive dependencies, the inertial lifecycle, and the
session seed boundary.

Back to s18's closing words — now more concrete:

> The harness's value is building an operable world.
> deepseek-harness shows that the world must be
> **revertible** (s19), **reactive** (s20), **inertial** (s21),
> and **recoverable** (s22).

Next step: open the [Cordis paper notes](../docs/cordis-paper-spatiotemporal-composability.md) —
every section of s19–s22 maps to a numbered theorem there.
