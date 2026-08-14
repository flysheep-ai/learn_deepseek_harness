# s18 — Full Harness（整合与验收）

**English version: [README.en.md](README.en.md)**

[s17](../s17_goal_loop/) → **s18**（终点）

> 这一章**不新增任何机制**。
> 它把 s01–s17 组装成一台完整的机器，然后用课程开头立下的标准验收。

---

## 这一章解决什么

前 17 章每个机制都单独成立。现在要回答两个问题：

1. 它们能**装进同一台机器**协同工作吗？
2. 这台机器能通过**最终验收**吗？

```
用户输入："帮我检查这个项目为什么测试失败，并修复它。"
```

Harness 从头到尾不知道这是一个 debugging task：

- 没有 `run_test()` / `analyze_error()` / `modify_code()` 硬编码步骤
- 没有"如果用户提到测试就 X"的分支
- 只有 `read / write / edit / grep / glob / bash`，
  以及 context / state / permission / session

模型自己走出的路径（demo 实录）：

```
glob("**/*.py")                     ← 先看项目结构
bash("python3 tests/test_maths.py") ← 跑测试，看到失败
read("calc/maths.py")               ← 读实现
read("tests/test_maths.py")         ← 读期望
edit("calc/maths.py", …)            ← 修复
bash("python3 tests/test_maths.py") ← 验证，通过
```

**这 6 步没有任何一步是 Harness 提前写好的。**
每一步都是模型在上一步的观察之后自己产生的。

---

## 整合架构

```
                 User
                  │
                  ▼
                Agent
                  │
           ┌──────┴──────┐
           ▼             ▼
       Session         Context
           │
           ▼
       Event Log
           │
           ▼
       Agent Loop
           │
           ▼
     Prompt Assembly
           │
           ▼
          LLM
           │
           ▼
       Tool Calls
           │
           ▼
 Tool Execution Pipeline
           │
 ┌─────────┼─────────┐
 ▼         ▼         ▼
Policy   Events   Tool Registry
           │
           ▼
  Capability Providers
           │
 ┌─────────┼─────────┐
 ▼         ▼         ▼
FS       Shell     Sandbox
```

这台机器在 demo 里的具体数字：

```
16 个插件：capabilities · identity · core-tools · session-log · validation
          · trace · redact · truncate · permission · timing · skills
          · tasks · jobs · subagent · compaction · goal
16 个工具 / 10 个 prompt 段 / 9 个 service / 12 个监听器 / 67 条事件
```

s14 那句"Harness 自己什么功能都没有"在这里有了最终形态：
**每一个机制都是一个可卸载的插件，或者一条可摘掉的监听器。**

---

## 验收怎么算通过

| 标准 | 出处 |
|---|---|
| 自主完成任务 | 模型自己走了 glob → bash → read → read → edit → bash |
| Harness 不知道任务类型 | 全 harness 本体搜索 `if task_type` / `call_xxx_agent` / `router.route` **零命中** |
| 每一步都从观察产生 | 每次工具调用前，模型都看得见上一步的结果 |
| 全程可追溯 | 67 条事件落盘，任意时刻可 replay |
| 离线可验证 | `--demo` 不需要任何 API key |

真实模型下跑一遍：

```sh
python s18_full_harness/code.py
> 帮我检查这个项目为什么测试失败，并修复它。
```

（你可以在一个真的坏了测试的项目目录里跑这个命令 ——
Agent 会自己走完全程，包括中间碰壁后的调整。）

---

## 为什么这样设计

### 为什么"整合"没有产生新代码

s18 的 harness 本体和 s17 **完全相同**。整合不是"写一个新系统"，
是"把 17 章各自建好的东西按顺序 use 起来"：

```python
h.use(CapabilityPlugin(...))       # s15
h.use(IdentityPlugin())            # s07
h.use(CoreToolsPlugin())           # s03 + s15
h.use(SessionLogPlugin())          # s05
h.use(ValidationPlugin())          # s03
h.use(PermissionPlugin(...))       # s04
h.use(TimingPlugin())              # s13
h.use(SkillPlugin(...))            # s08
h.use(TaskPlugin())                # s11
h.use(JobPlugin(...))              # s12
h.use(SubagentPlugin(...))         # s09 + s16
h.use(CompactionPlugin(...))       # s10
h.use(GoalPlugin(...))             # s17
```

这件事本身是 s14 的最终证明：**加一个机制 = 加一个插件，
已有的插件一行不用改。**

### 为什么有些机制这次"没用到"

demo 里模型**没有**用 subagent、task_write、bash_background、skill。

这不是遗憾，是**正确性**的体现：

- Harness 提供了这些能力（prompt 里可见、工具可调用）
- 模型判断这个任务用不上它们
- 于是它们安静地待在旁边

如果 Harness 强制走"先规划 → 派 subagent → 更新任务 → 后台跑测试"，
那就又变成替模型写工作流了。

**"没用到"是 Model decides 这条铁律的正常结果。**

### 为什么最终检查搜的是"代码形态"而不是裸词

```python
patterns = [r"if\s+" + t, "call_research" + "_agent", ...]
```

裸词搜索会命中检查代码自身（"task_type" 出现在检查逻辑里），
而且"搜词"抓不住真正的违规 —— 真正的违规是**决策分支的形态**：

```python
if task_type == "research": ...       # 形态，抓
task_type = "research"                # 赋值，无所谓
```

---

## 跑一下

```sh
python s18_full_harness/code.py --demo
python s18_full_harness/code.py --demo --debug
python s18_full_harness/code.py       # 真实模型完整验收
```

demo 的五幕：组成 → 验收 → 修复结果 → 机制清单 → 禁令检查。

---

## 与上一章相比发生了什么

| | s17 | s18 |
|---|---|---|
| harness 本体 | — | **与 s17 完全一致** |
| demo | goal 三轮生命周期 | **自主修复失败测试** |
| 验收 | — | 课程开头立下的标准 |
| 检查 | — | 禁令的代码形态扫描 |

---

## 真实系统里还有什么

这台 800 行核心的机器距离 DeepSeek Harness（7400+ 文件）还差什么？

- **流式输出**（`assistant/chunk` 逐 token 落盘回放）
- **并发工具执行**（`isConcurrencySafe` 分类 + rolling pool）
- **真正的沙箱**（Landlock / seccomp，而不是 provider 里的路径检查）
- **多 provider 注册表**（subagent seam 按名挂多个 provider）
- **配置驱动的装配**（profile 是 YAML 不是 elif）
- **凭据 / 遥测 / 标题生成 / 附件 / LSP / 终端 / Web UI**

但请注意：那些东西**没有一个是新概念**。
它们都是这 18 章的概念在工程尺度上的重复应用。
课程的目的已经达到了 —— 你现在读 dsh 的文档，每一页都能对上号。

---

## 自己动手改

1. **让验收任务更难**
   在 `build_buggy_project` 里埋两个 bug（除零 + 一个别处的错误），
   看模型怎么自己调整。

2. **换一个 profile 跑验收**
   `--profile minimal` —— 没有任务、没有团队、没有压缩。
   模型还能完成吗？哪些机制是"必需"，哪些是"锦上添花"？

3. **换一个世界跑验收**
   把 `fs` 换成 `MemoryFileSystem`，看验收任务变成什么样子。

4. **摘掉一个你认为无关的插件**
   比如 `TruncatePlugin`，然后制造一个超长输出的工具调用。

5. **读一遍 s18 的 session.jsonl**
   67 条事件，对照 s05–s17 每一章的概念，逐条说出它属于哪个机制。

---

## 终点之后

回到课程开头的那句话：

> Agent 的智能主要来自模型。
> Harness 的价值不是替模型写死思考流程，
> 而是为模型构建一个拥有工具、环境、上下文、状态、权限和反馈的可操作世界。

18 章走完，你应该能回答：

- 为什么 Agent Loop 只有一个 `while`
- 为什么 messages 不是真相
- 为什么权限住在管线上而不是 loop 里
- 为什么压缩是 shadow 而不是 delete
- 为什么"一切皆插件"的价值是边界
- 为什么换 provider 等于换世界
- 为什么 Harness 提供协作机制但不编写协作策略
- 为什么目标是持久状态而不是 `while not done`

**建议下一步**：打开 [deepseek-harness 的文档](https://github.com/deepseek-ai/deepseek-harness/tree/main/docs)，
你会发现每一页都在和这 18 章对话。
