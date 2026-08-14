# learn-agent-harness

> A **runnable** course on Agent Harness internals.
> Starting from a 60-line script, watch chapter by chapter how a modern coding
> agent's harness is **forced into existence by real problems**.

![CI](https://github.com/flysheep-ai/learn_deepseek_harness/actions/workflows/ci.yml/badge.svg) ![Python](https://img.shields.io/badge/Python-3.11%2B-blue) ![Chapters](https://img.shields.io/badge/chapters-18-orange) ![Tests](https://img.shields.io/badge/tests-20%20passing-green) ![Offline](https://img.shields.io/badge/demo-offline%20%E2%80%94%20no%20API%20key-lightgrey) ![Deps](https://img.shields.io/badge/dependencies-httpx%20only-brightgreen) ![License](https://img.shields.io/badge/license-MIT-yellow)

**English primary · [中文版请见 README.cn.md](README.cn.md)**

A runnable, progressive course on **agent harness internals**: how a modern coding
agent (Claude Code / DeepSeek Harness style) actually works under the hood — from a
60-line agent loop to a full pluggable harness with session event log, tool pipeline,
permission, subagents, plugin system, capability seams, and a goal loop. Every chapter
is a complete, self-contained, offline-runnable Python file.

`agent-harness` · `llm-agents` · `tool-calling` · `ai-coding-agent` · `plugin-system` · `event-sourcing` · `educational`

---

## What this is

A typical agent harness looks like this:

```
 User ──▶ Agent Loop ──▶ LLM ──▶ Tool Calls ──▶ Filesystem / Shell / Sandbox
            │  ▲
            ▼  │
        Session (event log)
```

Plenty of frameworks exist (LangChain, LangGraph, Claude Code, DeepSeek Harness),
but they are tens of thousands of lines — hard to read, harder to modify.

This project teaches you **what really happens underneath those frameworks** —
not through papers, but through 18 Python files, **one concept per chapter,
each fully runnable on its own**.

### Who this is for

- Developers who know Python, have called an LLM API, and used tool calling,
  but don't understand how a harness actually works
- Anyone who wants to read DeepSeek Harness / Claude Code architecture docs
  without getting lost (after this course, every page of dsh's
  `architecture.md` will map to a chapter here)
- Anyone building their own agent framework who doesn't want to
  dig through tens of thousands of lines of archaeology
- Anyone who wants a systematic understanding of: **agent loop / tool registry /
  session event log / permission / context compaction / subagent / plugin system /
  capability seam / goal loop**

## Design principles

| Principle | Meaning |
|---|---|
| Teaching value > feature count | This is a textbook, not a production framework |
| Clarity > architectural flair | Every abstraction is triggered by a concrete pain point |
| Progressive evolution > up-front design | Let the structure break first, then fix it — you'll see why the architecture grows |
| **Model decides** > harness hardcodes | The harness builds an operable world for the model; it does not script the model's thinking |
| Runnable > slideware | Every chapter runs offline: `python code.py --demo` |

---

## The learning path

```
Part 1: How an agent runs
──────────────────────────
 s01 agent_loop          A conversation loop — and why it's not an agent yet
 s02 tool_use            The first tool; the inner step loop is born
 s03 tool_registry       if/elif → Tool / Schema / Registry / Executor
 s04 permission          pre → execute → post pipeline; permission lives in pre
 s05 session_event_log   messages stop being the truth; the event log takes over
 s06 turn_and_step       One user input ≠ one model call

Part 2: How an agent manages Context / State / Task
──────────────────────────
 s07 prompt_assembly     The system prompt is a runtime artifact, not a constant
 s08 skill_loading       Progressive disclosure: catalog always, body on demand
 s09 subagent            Context isolation + restricted action space
 s10 context_compaction  Compaction shadows the projection, never the log
 s11 task_system         Tasks are harness state, not model memory
 s12 background_jobs     Synchronous tool call vs asynchronous job

Part 3: Why industrial harnesses need Event / Plugin / Capability / Isolation
──────────────────────────
 s13 event_bus           Permission/logging/metrics move out of the loop into listeners
 s14 plugin_system       Context / Registry / Plugin — everything is a plugin
 s15 capability_seams    Definition / Provider / Consumer
 s16 agent_team          spawn / send / receive / status; strategy belongs to the model
 s17 goal_loop           A goal is persistent state, not a `while not done`
 s18 full_harness        Integration, verified by autonomously fixing failing tests
```

Each chapter answers one concrete question:

- **Part 1**: What actually happens during one user input? Where do the agent's
  "memory", "actions", and "boundaries" live?
- **Part 2**: What happens when context overflows? Does compaction erase your plan?
  Why should a 5-minute test block the whole loop?
- **Part 3**: Why do industrial harnesses need Event / Plugin / Capability?
  How can "switch to a sandbox" not mean "rewrite six tools"?
  How do multiple agents cooperate without the harness scripting their workflow?

---

## Quick start

```sh
# Only one dependency: httpx (for real models)
pip install -r requirements.txt

# Any chapter runs offline — no API key needed
python s01_agent_loop/code.py --demo
python s10_context_compaction/code.py --demo --debug
python s18_full_harness/code.py --demo

# Connect a real model (OpenAI-compatible API or Anthropic)
cp .env.example .env     # fill in LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
python s18_full_harness/code.py
> Help me find out why the tests are failing and fix them.

# Run the test suite
python3 -m unittest discover tests
```

### Environment variables

```sh
LLM_PROVIDER=openai            # openai | anthropic
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=deepseek-chat
```

You don't need any of it to learn — every chapter's `--demo` runs on an offline
scripted model (`ScriptedProvider`).

---

## Repository layout

```
learn-agent-harness/
├── README.md               ← you are here (English)
├── README.cn.md            ← 中文版
├── DESIGN.md               ← research findings + course design decisions
├── harness_llm.py          ← the only shared file: the model access layer
│                             (contains zero harness logic)
├── requirements.txt        ← httpx only
├── tests/                  ← per-chapter smoke tests + deterministic mechanism tests
├── docs/                   ← paper notes (Cordis paper reading, in Chinese)
├── s01_agent_loop/         code.py + README.md（中文）+ README.en.md
├── s02_tool_use/           code.py + README.md（中文）+ README.en.md
│   …
└── s18_full_harness/       code.py + README.md（中文）+ README.en.md + skills/
```

**Why one `code.py` per chapter instead of a shared `src/`?**

Because importing a shared library hides the learning process:

```python
from src.agent import Agent    # ❌ you never see how Agent grew
```

This project prefers duplication: every chapter shows the **complete, minimal,
runnable implementation**. Diffing two adjacent chapters is the exact answer to
"what did this chapter add".

The single exception is `harness_llm.py` — HTTP transport is not harness mechanics,
so it's not part of the lesson.

Every chapter has **bilingual READMEs** (`README.md` in Chinese — the course text,
`README.en.md` in English), following the same structure:

```
The problem the last chapter left → what this chapter solves → the core concept
→ minimal architecture diagram → run it → why it's designed this way
→ what changed vs the previous chapter → what real systems do on top
→ try it yourself → the next chapter (ends with a question)
```

---

## Two iron laws running through all 18 chapters

### 1. Model-visible means logged

(Formalized in s05; every chapter from s05 to s18 pays its debt.)

> Anything that can reach a model request must be reconstructable from the log.

`messages` is not the truth — it is a **projection** of the event log. Recovery,
replay, forking, compaction, and audit all rest on this law.

### 2. Model decides. Harness enables

(Formalized in s02; most easily violated in s16; finally checked in s18.)

> The agent's intelligence comes from the model. The harness's value is not to
> script the model's thinking, but to build the model an operable world of tools,
> environment, context, state, permissions, and feedback.

The harness provides: tools / context / state / observation / permission /
execution / persistence / isolation / communication.
The model decides: what to do next, whether to revise the plan, how to solve the problem.

The test suite scans the harness core to confirm there is no branch like
`if task_type == "research"` making decisions on the model's behalf.

---

## Relationship to the reference projects

- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
  — borrowed its **teaching method**: one concept per chapter, READMEs that start
  from pain, in-code markers for "new / unchanged", willing duplication.
- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
  — absorbed its **industrial design**: Session Event Log, Turn/Step vocabulary,
  the tool execution pipeline, Capability Seams, everything-is-a-plugin.
  But not its Cordis framework — the same ideas are expressed here with a 30-line
  EventBus + PluginContext.

This project is **not** a fork, translation, or rewrite of either repository.
Research findings and design decisions: [DESIGN.md](DESIGN.md).

## Further reading

- [Cordis paper notes (Chinese): Revertible Effects and Reactive Coeffects](docs/cordis-paper-spatiotemporal-composability.md)
  — a Chinese reading guide to the 88-page formal paper underlying
  deepseek-harness's Cordis framework, including a side-by-side mapping to
  chapters s13/s14 of this course. Best read after finishing all 18 chapters.

## After the 18 chapters

You should be able to read industrial harness documentation now. Try:

1. Open [deepseek-harness's architecture.md](https://github.com/deepseek-ai/deepseek-harness/blob/main/docs/architecture.md)
   — every sentence should map to a mechanism from some chapter.
2. Explain Claude Code / Cursor behavior in this course's vocabulary:
   why it "remembers", why it forgets after compaction, why subagent work
   doesn't pollute the main context.
3. Modify s18: add a tool, add a plugin, swap a provider —
   each should touch exactly one place.

**The course's closing words** (also the ending of s18):

> The agent's intelligence comes from the model.
> The harness's value is not to script the model's thinking,
> but to build the model an operable world of tools, environment,
> context, state, permissions, and feedback.

## Contributing

Contributions are welcome — fixes to code or prose, new exercises, better
explanations, translations, or test coverage.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and house rules.
- All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Please open an issue before starting a large change, so we can align on direction.

## Security

Please report vulnerabilities privately instead of opening a public issue.
See [SECURITY.md](SECURITY.md).

## Support

For questions, ideas, and discussion, see [SUPPORT.md](SUPPORT.md).

## License

[MIT](LICENSE) © 2026 flysheep-ai
