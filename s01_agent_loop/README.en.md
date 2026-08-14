# s01 — Agent Loop

**中文版：[README.md](README.md)**

**s01** → [s02](../s02_tool_use/) → s03 → … → s18

> The code in this chapter is **not** an agent.
> Run it and see clearly what it cannot do — that is the premise of the next 17 chapters.

---

## What problem does this chapter solve

You want the model to do work for you:

> "Show me what files are in the current directory."

The model replies:

> "You can run `ls -la`. (Then paste the output back to me.)"

So you switch to a terminal, type `ls -la`, copy the output, paste it back into the
chat. The model says "next run `pytest`", so you type again, paste again.

**On every round trip, you are acting as the middle layer for the model.** You do
three things:

1. **Execute** the commands the model produces
2. **Feed** the results back into the model's context
3. **Decide** whether to continue

Those three things are the entire job of a harness. This chapter builds the
outermost frame first.

---

## The new core concepts

### Concept one: the Loop

```python
while True:
    user_input = input()
    messages.append({"role": "user", "content": user_input})
    reply = provider.chat(messages, system=SYSTEM)
    messages.append(reply.as_assistant_message())
    print(reply.text)
```

Five lines. No classes, no abstractions, no registries. This chapter needs none.

### Concept two: the model is stateless

This matters more than the loop itself.

The model **does not remember** the previous round. What we call "memory" exists
only because every request re-sends the **entire** `messages` history.

```
Request 1 sends: [user1]
Request 2 sends: [user1, assistant1, user2]          ← user1 sent again
Request 3 sends: [user1, assistant1, user2, assistant2, user3]
```

Therefore:

> **The `messages` list is the model's entire memory, and the one maintaining it
> is the harness, not the model.**

This is the harness's first responsibility, and the easiest to overlook:

> **The harness decides what the model can see on its next request.**

Every later chapter answers a variant of this one question: how tool results get
in (s02), how the system prompt is assembled (s07), when skill documents are
loaded (s08), what to do when nothing fits (s10), and how subagent garbage stays
**out** (s09).

---

## Minimal architecture diagram

```
   ┌──────┐   messages   ┌──────┐
   │ User │ ───────────▶ │ LLM  │
   └──────┘              └──────┘
       ▲                     │
       │       text          │
       └─────────────────────┘

   The harness does exactly one thing here: maintain messages
```

---

## Execution flow

```
You type     "show me what files are in this directory"
  ↓
messages    [{"role":"user", "content":"show me what files..."}]
  ↓
provider.chat(messages, system=SYSTEM)
  ↓
Reply(text="You can run `ls -la`……", tool_calls=())
  ↓
messages    [..., {"role":"assistant", "content":"You can run..."}]
  ↓
print, back to the top
```

---

## Run it

```sh
python s01_agent_loop/code.py --demo    # offline scripted model, no key needed
python s01_agent_loop/code.py           # real model (cp .env.example .env first)
```

`--demo` uses `harness_llm.ScriptedProvider` — a script-driven fake model.
All 18 chapters support `--demo`, so you can see the mechanics before deciding
whether to connect a real model.

---

## Why it's designed this way

### Why s01 doesn't define `class Agent`

Because nothing needs it yet.

The most important rule of this project is: **abstractions must be triggered by
pain, never invented in advance.**

If s01 handed you `Agent`, `AgentConfig`, and `AgentContext`, you would take them
as "the correct answer" without ever learning why they exist. But when, in s13,
you watch "adding tool-latency stats means editing the Executor, and adding a
sandbox means editing the Executor again", the EventBus that follows will feel
**inevitable** rather than prescribed.

So every chapter obeys:

> Simple first. Abstraction later.
> Let the structure break first, then fix it.

### Why the system prompt is a separate argument, not a message

```python
provider.chat(messages, system=SYSTEM)   # ← system is a parameter, not a message
```

Because the system prompt is not part of the conversation — it is a runtime
parameter re-assembled on every request.

Right now it's a constant string and the difference is invisible. But in s07 it
becomes: base instructions + current tool list + working directory + loaded
skills + task state — potentially different on every step. Mixing it into the
history would make that impossible to untangle.

---

## What changed vs. the previous chapter

This is the first chapter, so there is no previous one. But vs. "calling the API
directly":

| | Calling the API directly | s01 |
|---|---|---|
| Memory | every request independent | `messages` accumulates history |
| Loop | one Q, one A | `while True` |
| Ability to act | none | **still none** |

---

## What real systems do on top

What we omit here (later chapters or industrial implementations add it back):

- **Streaming**: real harnesses render tokens as they arrive; DeepSeek Harness
  even logs every chunk (`assistant/chunk`) for replay. This course is
  non-streaming throughout because streaming is orthogonal to every chapter's
  main concept.
- **Retry & error recovery**: model requests time out, return 429s, overflow
  context. We just wrap failures as `LLMError`.
- **Token accounting**: `Reply.usage` is already carried, but s01 has no
  consumer for it. s10 will use it.

---

## Try it yourself

1. **See the model's memory**
   Add `print(len(messages), messages)` before `provider.chat`, run three rounds,
   and confirm request 3 really re-sends round 1.

2. **Cut the memory**
   Change `provider.chat(messages, ...)` to `provider.chat(messages[-1:], ...)`
   and ask "what did I just ask?". Watch the amnesia — it proves the memory
   really lives in the harness's hands.

3. **Make the fake model say something else**
   Edit the text in `DEMO_SCRIPT` and re-run `--demo`.
   Also look at `harness_llm.ScriptedProvider`: it records every request's
   `messages / tools / system` in `self.seen` — the tests in later chapters all
   assert "what the model actually saw".

---

## Next chapter

The model can only **say** `ls -la`, not run it. The command's output never
enters `messages`, so the model never sees feedback from the world — it is
guessing blind.

Can the person who "runs the command and pastes the result back" be replaced by
code?

And if so — when does the loop stop? When the model finishes talking, or when it
still wants to act?

→ [s02 — Tool Use](../s02_tool_use/)
