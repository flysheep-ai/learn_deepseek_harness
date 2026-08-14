# DESIGN.md — learn-agent-harness

本文件记录：**调研结论**、**课程设计决策**、**不做什么**。

它是这个项目的设计契约。写代码之前先写它，是为了避免后面 18 章各写各的。

---

## 0. 一句话定位

> 这不是一个 Agent 框架，是一本可以运行的 **Agent Harness 教材**。
>
> 读者从 60 行代码开始，逐章看到一个现代 Coding Agent 的 Harness 是**怎么被问题逼出来的**。

衡量标准只有四个：`Readable` / `Runnable` / `Hackable` / `Understandable`。

功能数量不是指标。

### 0.1 与一般 harness 教程的差别（v2 定位）

前 18 章（s01–s18）搭建的是**任何** Agent Harness 都需要的骨架。
但从 s19 起，课程聚焦 deepseek-harness 里**与其他项目不同的内容**——
那些一般教程根本不讲、却是 dsh 真正的工程精华：

| 章 | dsh 的独门内容 | 出处（dsh） |
|---|---|---|
| s19 | 可逆效应：注册返回逆、accumulator 追踪、LIFO 恢复、独立性（乱序撤回） | Cordis 论文 §3.1 |
| s20 | 反应式 coeffect：依赖满足性**运行时重判**（activating/deactivating/neutral）、单源纪律、级联卸载的三段顺序 | Cordis 论文 §3.2/§4.3 |
| s21 | 目标视图驱动的生命周期：target vs committed view、惯性状态机、失败先恢复再记录 | Cordis 论文 §4.2/§4.3 |
| s22 | 会话生命周期：session/end-seed 种子边界、fork、goal activation（armed/disarmed）、派生缓存 | dsh session.md / goal.md |

s19–s22 每一章都**回指**前面某一章："你已经在用这个机制了，
只是没人告诉你它的名字和它为什么是对的。"

---

## 1. 调研：learn-claude-code

仓库：`shareAI-lab/learn-claude-code`（17 章，Anthropic SDK，Python）

### 1.1 目录事实

```
s01_agent_loop/     code.py  137 行
s02_tool_use/       code.py  189 行
s03_permission/     code.py  241 行
s04_hooks/          code.py  255 行
s05_todo_write/     code.py  347 行
s06_subagent/       code.py  361 行
s07_skill_loading/  code.py  359 行
s08_context_compact/code.py  479 行
s09_memory/         code.py  756 行
s10_task_system/    code.py  530 行
s11_background_tasks/        498 行
s12_cron_scheduler/          768 行
s13_agent_teams/            1794 行
s14_mcp_plugin/              529 行
s15_integrated_harness/     3061 行
s16_workflow_runtime/        874 行
s17_goal_loop/               882 行
```

每章一个 `code.py` + 三语 README，各章独立可运行，允许大量重复代码。

### 1.2 它为什么好学（值得学的教学方法）

1. **一章一个概念，且概念本身有名字**。章节名就是 Harness 术语（`permission` / `subagent` / `compaction`），读者读完能带走词汇表。
2. **README 从"痛点"开局，不从"API"开局**。s01 的开头不是"介绍 Agent Loop"，而是"你把 bash 输出手动粘回对话框——每个来回你都在做中间层"。**先让读者感到疼，再给解药。**
3. **代码内注释标注了『这一章新增了什么』**。s02 的 `code.py` 里直接写着：

   ```python
   # -- From s01 (unchanged) --
   # -- New in s02: four tools --
   # s01: output = run_bash(block.input["command"])
   # s02: output = TOOL_HANDLERS[block.name](**block.input)
   ```

   读者不需要 diff 工具就能看出演化。**这一条我们必须继承。**
4. **拒绝过早抽象**。s01 的工具执行就是 `run_bash(block.input["command"])` 一行硬编码，没有 Registry、没有 Protocol、没有 dataclass。抽象在被需要时才出现。
5. **README 结尾用问句引向下一章**，而不是"下一章我们将介绍 X"。s01 结尾："模型手里只有 bash，读文件要 `cat`，写文件要 `echo >`，又丑又容易出错。s02 给它 5 个真正的工具，会发生什么？"

### 1.3 它的局限（我们要补的）

| 局限 | 说明 |
|---|---|
| 绑死 Anthropic SDK | `from anthropic import Anthropic`，`response.stop_reason`、`block.type` 是 SDK 私有形状。读者学到的是 SDK，不是 Harness。 |
| 没有 Session Event Log | messages 数组从头到尾就是那个 `list`。读者不会意识到"上下文可以从事实日志派生"。 |
| 没有 Turn / Step 词汇 | 只有一个 `while True`，读者无法把"一次用户输入"和"一次模型调用"区分开。 |
| 没有 Capability Seam | 文件系统、shell 直接调用 `pathlib` / `subprocess`，无法替换。 |
| 后期章节膨胀 | s13 1794 行、s15 3061 行，单文件已经超出"可通读"范围。 |
| 需要真 API key 才能跑 | 无法离线验证，也无法写确定性测试。 |

---

## 2. 调研：deepseek-harness

仓库：`deepseek-ai/deepseek-harness`（TypeScript，monorepo，~7400 文件，50+ packages）

主要读的是 `docs/`：`architecture.md`、`agent-lifecycle.md`、`tool-execution-pipeline.md`、`capability-seams.md`、`cordis-primer.md`、`glossary.md`，以及 `docs/subsystems/` 下的 `session` / `tools` / `subagent` / `goal` / `skills` / `jobs` / `compaction` / `system-prompt`。

### 2.1 值得移植的六个工业设计

#### (1) Session Event Log 是唯一事实来源

> **Model-visible means logged.**
> Anything that reaches a model request must be reconstructable from the log.

`Session` 是 append-only 的 `SessionEvent` 日志。LLM 的 messages **不存储**，而是 `deriveMessages()` 从日志投影出来的。fork / resume / transcript / telemetry / persistence 全部从这一条流派生。

配套概念 `SurfaceEventType`：只有会产生 message 的事件才在"surface"上。`todo/write`、`request/header`、`compaction/*` 是 **log-only**，进日志但不进模型上下文。

这是整个 dsh 里最有教学价值的一条。**→ 我们的 s05。**

#### (2) Turn / Step / Round 三级词汇

`glossary.md` 给了精确定义：

- **step** — 一次模型请求 + 它引发的工具执行。
- **turn** — 一次 admitted input 的排空；包含 **零个或多个** step。
- **round** — 外层策略迭代（goal round / Ralph round），不等于 turn。

"turn 可以包含零个 step"（pre-step 被拒绝时）这个细节，恰好说明了为什么 turn 和 step 必须是两个概念。**→ 我们的 s06。**

#### (3) 工具执行是 waterfall 管线，不是 if/elif

`tool-execution-pipeline.md` 的实际顺序：

```
tool/call (先记日志，再执行)
  → tools/pre-execute  waterfall   (hooks / permission / sandbox)
  → monotonic guards               (deny 或 abstain)
  → ctx.approval                   (一次性询问)
  → tools/execute      waterfall   (around dispatch: timeout / retry / metrics)
      → 工具本体 execute()
  → tools/post-execute waterfall   (accept / block / replace / add context)
  → finalizeContent
  → tools/result                   (frozen 最终结果)
→ tool/result 事件
```

关键点：**权限不是 Agent Loop 里的一个 if，是管线上的一个 listener。** 换权限策略不需要动 loop。

`cordis-primer.md` 的 waterfall 语义：listener 收到 `(...args, next)`，调 `next()` 委派下去，不调就短路。这正是"策略型 listener 拥有决定权，观察型 listener 必须委派"。**→ 我们的 s04（管线形状）+ s13（变成事件）。**

#### (4) Capability Seam = 三个角色

> A **seam** is a swappable capability with three roles: a **Service Definition**, one or more **Service Providers**, and one or more **Consumers**.
> The seam is the complete capability, never one role.

canonical example：`dsh-shell`（Definition）/ `dsh-bash-local` + `dsh-bash-sandbox`（Providers）/ `dsh-tool-bash`（Consumer）。

价值那句话说得极好：

> Filesystem and subprocess providers share one execution world, so pointing them at a remote sandbox moves Bash, PTY, and LSP with them, **with no provider forks**.

换一个 provider，整个产品跟着换。**→ 我们的 s15。**

#### (5) Everything is a plugin（Cordis）

五个想法：plugin 是实现 Service 的对象；context 是 service 仓库（`ctx.tools` / `ctx.llm` / `ctx.sessions`）；用 `inject` 声明依赖而不是手写启动顺序；typed events 通信；**注册是可逆的 effect**（卸载时自动 unwind）。

> There is no privileged core to patch.

**→ 我们的 s14，但只取 `Context` / `Registry` / `EventBus` / `Plugin` 四个词，不复制 Cordis 的 loader / profile / bundle / patch 体系。**

#### (6) Scope：per-agent 的注册与限制

`glossary.md` 的 agent-scope：贡献（tool / prompt section / listener）要么 global 要么属于**恰好一个** scope；`tools.restrict` 对某个 scope 过滤全局工具集；被过滤掉的工具 **既不在 prompt 里，也拒绝执行**，和不存在完全一样。

这解释了 subagent 的真正价值不是"多调一次 LLM"，而是 **context isolation + 受限 action space**。**→ 我们的 s09。**

### 2.2 明确不移植的

| dsh 的东西 | 为什么不移植 |
|---|---|
| Cordis 全家桶（loader / profile / bundle / `cordis.patch.yml` / `!!js` 表达式） | 组合配置系统，和"理解 Harness"无关，纯增加阅读成本 |
| TypeScript 声明合并（`declare module` 扩展 `SessionEventMap`） | 语言特性，Python 里用 dict + 常量表达即可 |
| Branded ids / typert 运行时类型注册 | 类型安全工程，不是 Harness 原理 |
| `assistant/chunk` 逐 token 日志 + streaming | 重要但正交；我们的 provider 非流式，一次 `assistant/message` 就够 |
| Landlock / sandbox native 二进制 | 平台相关 |
| 并发工具执行（`isConcurrencySafe` / rolling pool / barriers） | 真实但会淹没管线本身的形状；串行执行足够讲清 pre/execute/post |
| `finalizeContent` / lossless JSON snapshot / invariants 注册表 | 防御性工程，不是概念 |
| ACP / MCP / LSP / PTY / web app | 集成层 |

**原则：dsh 的某个设计"很强大但不适合教学"时，实现它的极简版本，并在 README 里说明真实系统还做了什么。**

---

## 3. 本项目的设计决策

### 3.1 LLM 层：唯一的共享模块

18 章各自实现 harness 机制（允许重复），但 **HTTP 传输不是 harness 机制**。

根目录有且只有一个共享文件 `harness_llm.py`，提供：

```python
Reply          # 归一化的模型回复：text / tool_calls / usage / raw
ToolCall       # id / name / arguments(dict)
LLMProvider    # Protocol: chat(messages, tools, system) -> Reply
OpenAICompatProvider   # httpx，OpenAI 兼容 /chat/completions
AnthropicProvider      # httpx 直连 /v1/messages（不装 anthropic SDK）
ScriptedProvider       # 离线假模型，按脚本返回，供 --demo 与测试使用
get_provider()         # 读环境变量选择
```

它**不含任何 harness 逻辑**——没有 loop、没有 registry、没有 session。

`ScriptedProvider` 是关键设计：**每一章都能 `python code.py --demo` 在无 API key 的情况下完整跑通**，
读者第一次接触时不需要先去申请 key，测试也能确定性断言。

s15 会把 `LLMProvider` 重新当作一个 capability seam 讲一遍，那时才谈"为什么它是 Definition"。

### 3.2 消息格式：OpenAI 形状的裸 dict

不引入 `Message` dataclass。messages 就是：

```python
{"role": "user", "content": "..."}
{"role": "assistant", "content": "...", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

理由：读者已经认识这个形状；换成自定义类型会让"Harness 到底做了什么"被类型转换噪音掩盖。
`Reply` / `ToolCall` 是 provider **返回**方向的归一化，只是为了屏蔽 OpenAI / Anthropic 的差异。

### 3.3 每章结构

```
sXX_name/
    code.py       # 该阶段完整、最小、可运行的实现
    README.md     # 中文（本项目主语言）
```

`code.py` 里用统一的注释标记演化：

```python
# ── 沿用 sXX（未改动） ────────────────────────────
# ── sXX 新增：<概念> ─────────────────────────────
# ── sXX 改写：sXX-1 是 A，现在是 B，因为 C ────────
```

### 3.4 README 模板

```
# sXX — 标题

## 上一章留下的问题        ← 从疼痛开局
## 这一章解决什么
## 新增的核心概念
## 最小架构图
## 核心代码
## 执行流程
## 为什么这样设计          ← 最重要的一节
## 与上一章相比发生了什么   ← 逐条 diff
## 真实系统里还有什么       ← 诚实地说明我们简化了什么
## 自己动手改
## 下一章                  ← 用问句结尾
```

### 3.5 Trace / Debug

从 s06（Turn/Step 显式化）开始，每章支持 `--debug`，打印：

```
[turn 1 start]
  [step 1 start]
    → model request   messages=3 tools=5
    ← model reply     text=0 chars  tool_calls=1
    · tool pre        read(path=main.py)
    · tool result     ok  412 bytes
  [step 1 end]
  ...
[turn 1 end]  reason=natural-stop  steps=2
```

Harness 是抽象系统，**看不见就学不会**。

### 3.6 硬性禁令（贯穿全部 18 章）

> **Model decides. Harness enables.**

代码里不允许出现：

```python
if user_wants_to_code: ...
elif task_type == "research": ...
if error_count > 3: change_strategy()
```

Harness 只提供：tools / context / state / observation / permission / execution / persistence / isolation / communication。

"下一步做什么"永远是模型的输出，不是 Python 的 `if`。

s16（Agent Team）和 s17（Goal Loop）是这条禁令最容易被违反的地方，README 会专门检查。

---

## 4. 课程路线（18 章）

```
第一部分：Agent 怎么运行
  s01 agent_loop         对话循环 → 为什么它还不是 Agent
  s02 tool_use           第一个工具，内层 step 循环诞生
  s03 tool_registry      if/elif → Tool / Schema / Registry / Executor
  s04 permission         执行管线 pre → execute → post，权限住在 pre
  s05 session_event_log  messages 不再是真相，事件日志才是
  s06 turn_and_step      一次输入 ≠ 一次模型调用

第二部分：Agent 怎么管理 Context / State / Task
  s07 prompt_assembly    system prompt 是运行时产物，不是常量
  s08 skill_loading      渐进式披露：先给目录，用时才加载
  s09 subagent           context isolation + 受限 action space
  s10 context_compaction 上下文压缩，surface 替换写回事件日志
  s11 task_system        任务是 Harness 外部状态，不在模型脑子里
  s12 background_jobs    同步 tool call vs 异步 job

第三部分：工业级 Harness 为什么需要 Event / Plugin / Capability / Isolation
  s13 event_bus          权限/日志/度量从 loop 里搬出去，变成 listener
  s14 plugin_system      Context / Registry / Plugin，everything is a plugin
  s15 capability_seams   Definition / Provider / Consumer
  s16 agent_team         spawn / send / receive / status，协作策略归模型
  s17 goal_loop          目标是持久状态，不是 while not done
  s18 full_harness       整合，并通过"自主修复失败测试"验收

第四部分（进阶）：deepseek-harness 与其他项目不同的内容
  s19 revertible_effects  注册返回逆 = 可逆效应（track/accumulator/LIFO/独立性）
  s20 reactive_coeffects  依赖运行时重判 + 级联卸载三段（停供→守卫→撤逆）
  s21 inertial_lifecycle  target vs committed 驱动 + 惯性 + 失败先恢复再记录
  s22 session_lifecycle   end-seed 种子边界 / fork / activation / 派生缓存
```

每章相对上一章的**新增触发点**（即"上一章为什么不够用"）：

| 章 | 上一章的具体痛点 |
|---|---|
| s02 | 模型只能*说*出 `ls`，不能执行；人肉粘贴结果 |
| s03 | `if name == "bash"` 加到第 5 个工具时，schema 和实现已经对不上了 |
| s04 | 模型可以 `rm -rf`；Registry 给了能力却没有任何约束 |
| s05 | 崩溃后会话全丢；`messages` 里塞了 UI 状态；无法回答"第 3 步到底发生了什么" |
| s06 | 日志里 `tool/result` 一片，但分不清哪些属于同一次模型调用；无法做 per-turn 预算 |
| s07 | `SYSTEM_PROMPT` 已经 200 行，加一个工具要改三处 |
| s08 | 把 git / python 规范全塞进 prompt，8k token 里 90% 用不上 |
| s09 | 一次大搜索把 60k token 垃圾灌进主上下文，之后每一步都在为它付费 |
| s10 | 上下文撑爆；直接截断会切断 tool_call / tool_result 配对 |
| s11 | 长任务里模型忘记自己答应过的第 3 件事 |
| s12 | `pytest` 跑 5 分钟，整个 loop 卡死 |
| s13 | 加一条"记录工具耗时"要改 Executor；加 sandbox 又要改；loop 成了万能修改点 |
| s14 | 事件监听器散落在 `main()` 里，没法整体装卸；关掉权限要注释 5 行 |
| s15 | 想让工具跑在远程沙箱，得改每一个工具的实现 |
| s16 | 单个 subagent 不够；但一写 `if task_type == "research"` 就是 Harness 替模型思考 |
| s17 | 用户关掉终端，目标就没了；模型自己判断"完成了吗"没有预算约束 |
| s18 | 前面 17 个机制各自为政，需要一次真实任务把它们串起来 |

---

## 5. 验收标准

1. 每章 `python sXX_*/code.py --demo` 离线跑通。
2. `python3 -m unittest discover tests` 全绿（每章一个 smoke test，关键机制有单测）。
3. s18 在真实模型下能完成：
   > "帮我检查这个项目为什么测试失败，并修复它。"
   Harness 全程不知道这是一个 debugging task；`read / write / edit / grep / glob / bash` 之外没有任何任务专用逻辑。
4. 全库 grep `task_type` / `intent` / `router` / `planner`，确认没有替模型做决策的分支。
5. 抽查任意相邻两章 `diff`，能一眼看出新增机制。

---

## 6. 明确不做

- 不做 streaming（正交，且会淹没每一章的主概念）
- 不做并发工具执行（同上）
- 不做 MCP / ACP / LSP 集成
- 不做 Web UI
- 不做真实 sandbox（s15 用 `MemoryFileSystem` 演示 provider 替换即可）
- 不做多语言 README（中文为主）
- 不做 `src/` 共享包（`harness_llm.py` 是唯一例外，且不含 harness 逻辑）

---

## 7. 最终自审（已完成，2026-08-14）

### 是否过度 Hardcode —— 通过

全库（18 个 harness 本体）搜索：

- `if task_type` / `elif task_type` / `call_research_agent` / `intent_classif` / `router.route` —— **零命中**
- `class Planner / Router / Workflow` —— 零命中

命中的只有注释和演示文本（讲解禁令本身的文字）。

### 是否过度抽象 —— 通过

全部 Protocol 及其实现数：

| Protocol | 实现 | 数量 |
|---|---|---|
| `LLMProvider`（harness_llm.py） | OpenAI / Anthropic / Scripted | 3 |
| `Plugin` | 13 个插件 | 13 |
| `FileSystem` | Local / Memory | 2 |
| `Shell` | Local / DryRun | 2 |

没有"一个实现五层接口"的情况。

### 是否真的渐进 —— 通过

行数轨迹（每章 code.py）：134 → 239 → 435 → 594 → 722 → 811 → 1030 →
1225 → 1471 → 1776 → 1976 → 2236 → 2563 → **2029（s14 重构降了 500 行）** →
2232 → 2424 → 2602 → 2676。

每章 README 的「上一章留下的问题」都指向前一章的具体痛点；
相邻两章 diff 只差一个机制。

### 是否真的可以学习 —— 通过

- 18 章 × `--demo` 全部离线跑通（无 API key）
- 20 个确定性测试全绿（unittest，无 pytest 依赖）
- s18 完成验收：自主修复失败测试，6 步全部由模型产生
- 每章 README 含「自己动手改」实验清单
