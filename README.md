# learn-agent-harness

> A **runnable** course on Agent Harness internals.
> Start from a 60-line script and watch, chapter by chapter, how a modern
> coding agent's harness is **forced into existence by real problems**.

![CI](https://github.com/flysheep-ai/learn_deepseek_harness/actions/workflows/ci.yml/badge.svg) ![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue) ![23 chapters](https://img.shields.io/badge/chapters-23-orange) ![22 tests passing](https://img.shields.io/badge/tests-22%20passing-green) ![offline demo](https://img.shields.io/badge/demo-offline%E2%80%94no%20API%20key-lightgrey) ![deps](https://img.shields.io/badge/deps-httpx%20only-brightgreen) ![License](https://img.shields.io/badge/license-MIT-yellow)

**English · [中文版 README.cn.md](README.cn.md)**

A runnable, progressive course on **agent harness internals** — how a modern
coding agent (Claude Code / DeepSeek Harness style) works under the hood, from
a 60-line agent loop to a full pluggable harness. 23 chapters, one concept per
chapter, **every chapter a complete self-contained Python file that runs
offline** (`python code.py --demo`, no API key).

`agent-harness` · `llm-agents` · `tool-calling` · `ai-coding-agent` · `plugin-system` · `educational`

---

## What this is

```
 User ──▶ Agent Loop ──▶ LLM ──▶ Tool Calls ──▶ Filesystem / Shell / Sandbox
            │  ▲
            ▼  │
        Session (event log)
```

Frameworks like LangChain, LangGraph, Claude Code, and DeepSeek Harness are
tens of thousands of lines — hard to read, harder to modify. This course
teaches **what really happens underneath them**: agent loop / tool registry /
session event log / permission / context compaction / subagent / plugin system
/ capability seam / goal loop — and the parts that make deepseek-harness
*genuinely novel*: revertible effects, reactive dependencies, inertial
lifecycles, and self-extension.

**For**: developers who know Python, have called an LLM API, and used tool
calling, but want to understand how a harness actually works. After the
course, every page of DeepSeek Harness's `architecture.md` maps to a chapter
here.

---

## What makes deepseek-harness different

A generic harness tutorial teaches the 60-line loop this course opens with —
`while True: call the model, run its tools, append messages`. What it rarely
teaches is *why an industrial harness looks nothing like that loop*. Most of
the distance is **not "more features"**, but a handful of **named structural
decisions** — the real subject of this course:

| Distinctive decision | Why it isn't obvious | Chapters | Minimal form here |
|---|---|---|---|
| **The event log is the truth** | `messages` looks like the memory, but it's only a *projection*; what's actually stored is an append-only log | s05 | `Session` + `derive_messages()` |
| **Turn / Step / Round** | "one input" ≠ "one model call"; without the words you can't reason about budgets, replay, or rejected turns | s06 → s17 | `run_turn()` + inner step loop |
| **Permission is a listener, not an `if`** | tool execution is a *waterfall* (`pre → execute → post`); policy hangs off it, addable/removable without touching the loop | s04 → s13 | a 6-line `EventBus.waterfall` |
| **Capability seams** | Definition / Provider / Consumer — swap one provider and the whole product follows, no provider forks | s15 | `FileSystem` / `Shell` Protocols + Local / Memory / DryRun |
| **Everything is a plugin** | there is no privileged core to patch; a feature is one unit, mounted whole and unmounted whole | s14 | `PluginContext` + reverse-order disposers |
| **Reversible effects** | registration returns its own inverse, so composite cleanup is automatic — the reason plugins can hot-unload | s14 → [s19](s19_revertible_effects/) | `on()/use()/register()` return a disposer |
| **Reactive coeffects** | dependencies are re-evaluated on *every* context change; unloading cascades stop-provide → guard → withdraw | [s20](s20_reactive_coeffects/) | `DependencyRuntime._reevaluate` |
| **Inertial lifecycle** | transitions run to completion before responding to new targets; failure recovers first, records second | [s21](s21_inertial_lifecycle/) | target vs committed view |
| **Scope** | a subagent's value is context isolation + a *restricted action space*, not "one more LLM call" | s09 | `registry.restricted()` |
| **Goal as persistent state** | a goal survives the terminal and is judged by the model, not by `while not done` | s17 → [s22](s22_session_lifecycle/) | `GoalStore` over the event log |
| **Self-extension** | the model inspects and modifies its **own runtime** mid-session | [s23](s23_self_extending/) | `harness_inspect/mount/unmount` |

These ideas are not folklore — they are grounded in a formal paper, **Cordis**
(*revertible effects* + *reactive coeffects*). This course re-expresses the
paper's core with a 30-line `EventBus` + `PluginContext`; the
[paper reading notes](docs/cordis-paper-spatiotemporal-composability.md) map
every Cordis concept back to a chapter, including what was deliberately
**not** ported. Reading path: [docs/](docs/README.md).

---

## The learning path

**Part 1 — how an agent runs**

| | | |
|---|---|---|
| **s01** [agent_loop](s01_agent_loop/) | a conversation loop — and why it's not an agent yet |
| **s02** [tool_use](s02_tool_use/) | the first tool; the inner step loop is born |
| **s03** [tool_registry](s03_tool_registry/) | `if/elif` → Tool / Schema / Registry / Executor |
| **s04** [permission](s04_permission/) | pre → execute → post pipeline; permission lives in `pre` |
| **s05** [session_event_log](s05_session_event_log/) | messages stop being the truth; the event log takes over |
| **s06** [turn_and_step](s06_turn_and_step/) | one user input ≠ one model call |

**Part 2 — how an agent manages context, state, and tasks**

| | | |
|---|---|---|
| **s07** [prompt_assembly](s07_prompt_assembly/) | the system prompt is a runtime artifact, not a constant |
| **s08** [skill_loading](s08_skill_loading/) | progressive disclosure: catalog always, body on demand |
| **s09** [subagent](s09_subagent/) | context isolation + restricted action space |
| **s10** [context_compaction](s10_context_compaction/) | compaction shadows the projection, never the log |
| **s11** [task_system](s11_task_system/) | tasks are harness state, not model memory |
| **s12** [background_jobs](s12_background_jobs/) | synchronous tool call vs asynchronous job |

**Part 3 — why industrial harnesses need Event / Plugin / Capability**

| | | |
|---|---|---|
| **s13** [event_bus](s13_event_bus/) | permission/logging/metrics move out of the loop into listeners |
| **s14** [plugin_system](s14_plugin_system/) | Context / Registry / Plugin — everything is a plugin |
| **s15** [capability_seams](s15_capability_seams/) | Definition / Provider / Consumer — swap the provider, swap the world |
| **s16** [agent_team](s16_agent_team/) | spawn / send / receive / status; strategy belongs to the model |
| **s17** [goal_loop](s17_goal_loop/) | a goal is persistent state, not a `while not done` |
| **s18** [full_harness](s18_full_harness/) | integration, verified by autonomously fixing failing tests |

**Part 4 — deepseek-harness's distinctive mechanisms (advanced)**

| | | |
|---|---|---|
| **s19** [revertible_effects](s19_revertible_effects/) | registration returns its inverse: track / accumulator / LIFO / independence |
| **s20** [reactive_coeffects](s20_reactive_coeffects/) | dependencies re-evaluated on every context change; the three-stage cascade |
| **s21** [inertial_lifecycle](s21_inertial_lifecycle/) | target vs committed view drives everything; inertia; failure recovers first |
| **s22** [session_lifecycle](s22_session_lifecycle/) | session/end-seed boundary, fork, goal activation (armed/disarmed), derived caches |
| **s23** [self_extending](s23_self_extending/) | inspect / mount / unmount — the model modifies its own runtime |

Each chapter ends by pointing at the pain the **next** chapter exists to fix.
Diff two adjacent chapters and you have the exact answer to "what did this
chapter add".

---

## Quick start

```sh
pip install -r requirements.txt        # one dependency: httpx

python s01_agent_loop/code.py --demo                 # any chapter runs offline
python s18_full_harness/code.py --demo --debug       # trace the inside of a turn
python s23_self_extending/code.py --demo             # the model modifies its own runtime

# real model (OpenAI-compatible API or Anthropic)
cp .env.example .env          # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
python s18_full_harness/code.py
> Help me find out why the tests are failing and fix them.

python3 -m unittest discover tests                    # 22 deterministic tests
```

No key is needed to learn — every chapter's `--demo` runs on an offline
scripted model (`ScriptedProvider`). See [.env.example](.env.example) for the
env variables.

---

## Two iron laws

1. **Model-visible means logged** (formalized in s05).
   Anything that reaches a model request must be reconstructable from the
   event log. `messages` is a *projection*, never the truth.
2. **Model decides. Harness enables** (formalized in s02, checked in s18).
   The harness builds an operable world of tools, context, state, and
   permissions — it never scripts the model's thinking. The test suite scans
   the core for `if task_type == "research"` style branches: zero hits.

---

## Repository layout

```
learn-agent-harness/
├── README.md / README.cn.md   ← bilingual entry points
├── DESIGN.md                  ← research findings + course design decisions
├── harness_llm.py             ← the only shared file: the model access layer
│                                (contains zero harness logic)
├── tests/                     ← per-chapter smoke tests + deterministic mechanism tests
├── docs/                      ← Cordis paper notes + reading index
├── s01_agent_loop/ … s23_self_extending/
│     each: code.py + README.md（中文）+ README.en.md
```

**Why one `code.py` per chapter instead of a shared `src/`?**
Because `from src.agent import Agent` hides the learning process — you never
see how Agent grew. This project prefers duplication: every chapter shows the
**complete, minimal, runnable implementation**. The single exception is
`harness_llm.py` — HTTP transport is not harness mechanics.

---

## Relationship to the reference projects

- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
  — borrowed its **teaching method**: one concept per chapter, READMEs that
  start from pain, in-code "new / unchanged" markers, willing duplication.
- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
  — absorbed its **industrial design**: Session Event Log, Turn/Step
  vocabulary, tool pipeline, capability seams, everything-is-a-plugin,
  self-extension. Not its Cordis framework — the same ideas are expressed here
  with a 30-line EventBus + PluginContext. See the concept → chapter map
  [above](#what-makes-deepseek-harness-different).

This project is **not** a fork, translation, or rewrite of either repository.
Design decisions: [DESIGN.md](DESIGN.md).

## Further reading

- [docs/README.md](docs/README.md) — index and suggested reading path.
- [Cordis paper notes (Chinese)](docs/cordis-paper-spatiotemporal-composability.md)
  — a reading guide to the 88-page formal paper underlying deepseek-harness's
  Cordis framework, mapping every concept to a chapter (best after s14–s23).

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
