# learn-agent-harness

**中文版 · [English version: README.md](README.md)**

> 一本**可以运行**的 Agent Harness 教材。
> 从 60 行代码开始，逐章看到现代 Coding Agent 的 Harness 是怎么被问题逼出来的。

![CI](https://github.com/flysheep-ai/learn_deepseek_harness/actions/workflows/ci.yml/badge.svg) ![Python](https://img.shields.io/badge/Python-3.11%2B-blue) ![Chapters](https://img.shields.io/badge/chapters-18-orange) ![Tests](https://img.shields.io/badge/tests-20%20passing-green) ![Offline](https://img.shields.io/badge/demo-offline%20%E2%80%94%20no%20API%20key-lightgrey) ![Deps](https://img.shields.io/badge/dependencies-httpx%20only-brightgreen) ![License](https://img.shields.io/badge/license-MIT-yellow)

**中文教程 · Chinese course with English keywords for searchability**

`agent-harness` · `llm-agents` · `tool-calling` · `ai-coding-agent` · `plugin-system` · `event-sourcing` · `educational`

A runnable, progressive course on **agent harness internals**: how a modern coding agent
(Claude Code / DeepSeek Harness style) actually works under the hood — from a 60-line
agent loop to a full pluggable harness with event log, tool pipeline, permission,
subagents, plugin system, capability seams, and a goal loop. Every chapter is a
complete, self-contained, offline-runnable Python file.

---

## 这是什么

一个 Agent Harness 通常长这样：

```
 用户 ──▶ Agent Loop ──▶ LLM ──▶ Tool Calls ──▶ 文件系统 / Shell / 沙箱
             │  ▲
             ▼  │
          Session（事件日志）
```

市面上有大量框架（LangChain / LangGraph / Claude Code / DeepSeek Harness），
但它们的代码量以万行计，读不懂也改不动。

这个项目教你**这些框架底下到底发生了什么** ——
不是通过论文，而是通过 18 个**每章一个概念、每章都能独立运行**的 Python 文件。

### 适合谁读

- 会 Python、调过 LLM API、用过 tool calling，但**不懂 Harness 原理**的开发者
- 想读懂 DeepSeek Harness / Claude Code 架构文档的人（读完全课程，dsh 的
  architecture.md 每一页都能对上号）
- 想自己写 Agent 框架，但不想从几万行代码里考古的人
- 想系统学习 **agent loop / tool registry / session event log / permission /
  context compaction / subagent / plugin system / capability seam / goal loop**
  等概念的人

### 设计原则

| 原则 | 含义 |
|---|---|
| 教学价值 > 功能数量 | 这不是生产框架，是教材 |
| 清晰度 > 架构炫技 | 每一行抽象都由具体的痛点触发 |
| 渐进演化 > 一次性设计 | 先让结构坏掉，再修它 —— 你会亲眼看到架构为什么长出来 |
| **Model decides** > Harness hardcodes | Harness 给模型一个可操作的世界，不替模型写思考流程 |
| Runnable > PPT 架构 | 每一章都 `python code.py --demo` 离线跑通 |

---

## deepseek-harness 到底「不同」在哪

普通的 harness 教程只会教你本课程开篇的那 60 行循环 ——
`while True: 调模型 → 跑工具 → 追加 messages`。它很少告诉你，
为什么工业级 harness 和这个循环长得完全不一样。而这段距离的绝大部分
**不是「功能更多」，而是少数几个有名字的结构性决策**。这些决策才是本课程真正要教的东西：

| 独特决策 | 为什么不显然 | 章节 | 本课程的最简形态 |
|---|---|---|---|
| **事件日志才是真相** | `messages` 看起来像记忆，其实只是*投影*；真正存储的是 append-only 日志 | s05 | `Session` + `derive_messages()` |
| **Turn / Step / Round** | 「一次输入」≠「一次模型调用」；没有这套词汇就无法谈论预算、回放、被拒绝的 turn | s06 | `run_turn()` + 内层 step 循环 |
| **权限是 listener，不是 `if`** | 工具执行是*瀑布管线*（`pre → execute → post`）；策略挂在管线上，增删都不用动 loop | s04 → s13 | 6 行 `EventBus.waterfall` |
| **Capability Seam** | Definition / Provider / Consumer —— 换一个 provider，整个产品跟着换，无需 provider 分叉 | s15 | `FileSystem` / `Shell` Protocol + Local / Memory / DryRun |
| **Everything is a plugin** | 没有需要 patch 的特权核心；一个特性就是一个单元，整体挂载、整体卸载 | s14 | `PluginContext` + 逆序 disposer |
| **可逆效应** | 注册返回自己的逆，复合清理自动推导 —— 插件能热卸载的根本原因 | s14 + [笔记](docs/cordis-paper-spatiotemporal-composability.md) | `on()/use()/register()` 返回 disposer |
| **Scope** | subagent 的价值是 context isolation + *受限 action space*，不是「再多调一次 LLM」 | s09 | `registry.restricted()` |
| **目标是持久状态** | 目标在关掉终端后仍在，由模型判定完成，而不是 `while not done` | s17 | 事件日志上的 `GoalStore` |

deepseek-harness 真正**独一无二**的地方，是这条链的最后一步：
这些想法不是江湖经验，而是有一篇形式化论文 —— **Cordis**（可逆效应 + 反应式 coeffect）做底。
本课程用 30 行的 `EventBus` + `PluginContext` 重新表达了论文的核心，
而[论文解读](docs/cordis-paper-spatiotemporal-composability.md)把每个 Cordis 概念
一一对应回章节 —— 包括我们**刻意没有移植**的部分（反应式 coeffect、fiber 生命周期、confluence 定理）。
阅读路径见 [docs/](docs/README.md)。

---

## 学习路线

```
第一部分：Agent 怎么运行
─────────────────────────
 s01 agent_loop          对话循环 → 为什么它还不是 Agent
 s02 tool_use            第一个工具，内层 step 循环诞生
 s03 tool_registry       if/elif → Tool / Schema / Registry / Executor
 s04 permission          执行管线 pre → execute → post，权限住在 pre
 s05 session_event_log   messages 不再是真相，事件日志才是
 s06 turn_and_step       一次输入 ≠ 一次模型调用

第二部分：Agent 怎么管理 Context / State / Task
─────────────────────────
 s07 prompt_assembly     system prompt 是运行时产物，不是常量
 s08 skill_loading       渐进式披露：先给目录，用时才加载
 s09 subagent            context isolation + 受限 action space
 s10 context_compaction  压缩压的是投影，不是日志
 s11 task_system         任务是 Harness 外部状态，不在模型脑子里
 s12 background_jobs     同步 tool call vs 异步 job

第三部分：工业级 Harness 为什么需要 Event / Plugin / Capability / Isolation
─────────────────────────
 s13 event_bus           权限/日志/度量从 loop 里搬出去，变成 listener
 s14 plugin_system       Context / Registry / Plugin，everything is a plugin
 s15 capability_seams    Definition / Provider / Consumer
 s16 agent_team          spawn / send / receive / status，协作策略归模型
 s17 goal_loop           目标是持久状态，不是 while not done
 s18 full_harness        整合，并通过「自主修复失败测试」验收

第四部分（进阶）：deepseek-harness 与其他项目不同的内容
─────────────────────────
 s19 revertible_effects  注册返回逆：track / accumulator / LIFO 恢复 /
                         独立性（乱序撤回）
 s20 reactive_coeffects  依赖满足性每次上下文变化都重判；
                         级联卸载三段（停供 → 守卫 → 撤逆）
 s21 inertial_lifecycle  target vs committed 视图驱动一切；惯性；
                         失败先恢复再记录
 s22 session_lifecycle   session/end-seed 种子边界、fork、
                         goal activation（armed/disarmed）、派生缓存
```

每章都回答一个具体问题：

- **前半部分**：一次用户输入到底发生了什么？Agent 的"记忆"、
  "行动"、"边界"分别住在哪里？
- **中间部分**：上下文装不下了怎么办？计划会不会被压缩冲掉？
  跑 5 分钟的测试为什么要卡死整个 loop？
- **后半部分**：为什么工业 Harness 要 Event / Plugin / Capability？
  怎么让"换一个沙箱"不意味着"改六个工具"？
  怎么让多个 Agent 协作而不替模型写工作流？
- **进阶篇（s19–s22）**：deepseek-harness 与其他项目**不同**的内容——
  可逆效应、反应式依赖、惯性生命周期、会话种子边界。
  Cordis 论文把这些形式化了；这里把它们做成可运行的章节。

---

## 快速开始

```sh
# 依赖只有一个：httpx（真实模型时用）
pip install -r requirements.txt

# 任何一章都可以离线跑（不需要 API key）
python s01_agent_loop/code.py --demo
python s10_context_compaction/code.py --demo --debug
python s18_full_harness/code.py --demo

# 连真实模型（OpenAI 兼容 API 或 Anthropic）
cp .env.example .env     # 填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
python s18_full_harness/code.py
> 帮我检查这个项目为什么测试失败，并修复它。

# 跑测试
python3 -m unittest discover tests
```

### 环境变量

```sh
LLM_PROVIDER=openai            # openai | anthropic
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=deepseek-chat
```

不填也能学 —— 每一章的 `--demo` 用离线假模型（`ScriptedProvider`）完整跑通。

---

## 目录结构

```
learn-agent-harness/
├── README.md               ← 你在这里
├── DESIGN.md               ← 调研结论 + 课程设计决策
├── harness_llm.py          ← 唯一的共享文件：模型访问层（不含任何 Harness 逻辑）
├── requirements.txt        ← 只有 httpx
├── tests/                  ← 每章 smoke + 机制确定性测试
├── s01_agent_loop/         code.py + README.md
├── s02_tool_use/           code.py + README.md
│   …
└── s18_full_harness/       code.py + README.md + skills/
```

**为什么每章一个 code.py，而不是一个共享的 src/？**

因为导入共享库会隐藏学习过程：

```python
from src.agent import Agent    # ❌ 你看不到 Agent 是怎么长的
```

这个项目宁可重复代码，也要让你在每一章看到**完整、最小、可运行的实现**。
前后两章 `diff` 一下，就是"这一章新增了什么"的准确答案。

唯一的例外是 `harness_llm.py`（HTTP 传输不是 Harness 机制，不是这门课要教的）。

每章的 README 结构统一：

```
上一章留下的问题 → 这一章解决什么 → 新增的核心概念 → 最小架构图
→ 跑一下 → 为什么这样设计 → 与上一章相比发生了什么
→ 真实系统里还有什么 → 自己动手改 → 下一章（用问句结尾）
```

---

## 两条贯穿全课程的铁律

### 1. Model-visible means logged

（s05 正式提出，s05–s18 每章都在还它的债）

> 凡是能进入模型请求的东西，都必须能从事件日志重建。

`messages` 不是真相，它是事件日志的一个**投影**。恢复、回放、分叉、
压缩、审计 —— 全部建立在这条铁律上。

### 2. Model decides. Harness enables

（s02 正式提出，s16 最容易被违反，s18 做最终检查）

> Agent 的智能主要来自模型。Harness 的价值不是替模型写死思考流程，
> 而是为模型构建一个拥有工具、环境、上下文、状态、权限和反馈的可操作世界。

Harness 提供：tools / context / state / observation / permission /
execution / persistence / isolation / communication。
模型决定：下一步做什么、是否修改计划、如何解决问题。

全库测试里有一条会扫描 Harness 本体，确认没有
`if task_type == "research"` 这类替模型决策的分支。

---

## 参考项目与本项目的关系

- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
  —— 学习了它的**教学方法**：一章一概念、README 从痛点开局、
  代码内标注"新增/沿用"、允许重复代码。
- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
  —— 吸收了它的**工业设计**：Session Event Log、Turn/Step 词汇、
  工具执行管线、Capability Seam、Everything is a plugin。
  但没有复制它的 Cordis 框架 —— 用 30 行 EventBus + PluginContext 表达同样的思想。
  概念 → 章节的对应关系见
  ["deepseek-harness 到底「不同」在哪"](#deepseek-harness-到底不同在哪)。

本项目**不是**上述任何一个项目的 fork / 翻译 / 简化版。
调研结论和设计决策见 [DESIGN.md](DESIGN.md)。

## 延伸阅读

- [docs/README.md](docs/README.md) —— 补充阅读的目录索引与推荐阅读路径。
- [Cordis 论文解读：可逆效应与反应式 Coeffect](docs/cordis-paper-spatiotemporal-composability.md)
  —— deepseek-harness 底层框架 Cordis 的形式化论文（88 页）的中文解读，
  含"与本课程的逐项对照"。读完 s13/s14/s15 后再看，效果最好。

## 参与贡献

欢迎贡献 —— 修代码或文字、补充练习、改进讲解、翻译、或补测试。

- 工作流程和规矩见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 所有参与者请遵守[行为准则](CODE_OF_CONDUCT.md)。
- 大改动请先开 issue 对齐方向。

## 安全

发现安全问题请私下报告，不要开公开 issue。见 [SECURITY.md](SECURITY.md)。

## 支持

有问题、想法或想讨论，见 [SUPPORT.md](SUPPORT.md)。

## License

[MIT](LICENSE) © 2026 flysheep-ai

---

## 读完 18 章之后

你应该能读懂工业 Harness 的文档了。试试：

1. 打开 [deepseek-harness 的 architecture.md](https://github.com/deepseek-ai/deepseek-harness/blob/main/docs/architecture.md)，
   每一句话应该都能对上某一章的机制。
2. 看 Claude Code / Cursor 的行为，用本课程的词汇解释它：
   为什么它能"记得"、为什么压缩后会忘事、为什么子任务不污染主上下文。
3. 改 s18：加一个工具、加一个插件、换一个 provider ——
   每件事都应该只动一处。

**课程的最后一句**（也是 s18 的结尾）：

> Agent 的智能主要来自模型。
> Harness 的价值不是替模型写死思考流程，
> 而是为模型构建一个拥有工具、环境、上下文、状态、权限和反馈的可操作世界。
