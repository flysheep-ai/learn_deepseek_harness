# s09 — Subagent

[s08](../s08_skill_loading/) → **s09** → [s10](../s10_context_compaction/) → … → s18

> Subagent 的价值**不是**"多调了一次 LLM"。
>
> 是 **Context Isolation**。

---

## 上一章留下的问题

用户问：

> "这个代码库里，鉴权逻辑分散在哪些文件？"

模型会 `grep`、`glob`、`read` 十几个文件。这些搜索结果 —— demo 里是 6177 字符，
真实项目里可能是 60000 token —— **全部灌进主上下文，而且永远留在那里**。

最终模型只需要给出一句结论。但那堆垃圾会跟着你走完整个会话：

- 后面每一步都要重新发送它们（**钱**）
- 后面每一步模型都要在里面找重点（**注意力**）
- 上下文很快就撑爆（s10 的问题）

---

## 这一章解决什么

让昂贵的探索发生在**另一个上下文**里，主上下文只收结论：

```
主 Agent 上下文                 子 Agent 上下文（独立、用完即弃）
┌────────────────┐             ┌──────────────────────────────┐
│ user: 鉴权在哪？ │             │ task: 找出鉴权相关文件         │
│ tool: spawn(…)  │──── spawn ─▶│ grep → 3000 字符              │
│ tool: "结论：    │             │ read → 1500 字符              │
│   auth/mw.py:5  │◀─── 结论 ───│ read → 1200 字符              │
│   api/deps.py:3"│  100 字符    │ "结论：auth/mw.py:5, deps:3"  │
└────────────────┘             └──────────────────────────────┘
   增长 4 条消息                    8 条消息随子会话一起丢弃
```

demo 的实测：

```
对照组（不用子 Agent）：主上下文 8 条消息 / 6177 字符
实验组（派 explorer）： 主上下文 4 条消息 /  190 字符
主上下文省下了 5987 字符（97%）
```

---

## 新增的核心概念

### 1. 子 Agent 有自己的 Session

```python
child = Session(path=parent_dir / f"{parent.id}_sub_{...}.jsonl")
```

这就是"隔离"的**物理实现**。它的 grep 结果、read 内容、走过的弯路
全部落在另一个文件里，主上下文的 `derive_messages()` 永远看不到。

注意子会话是**独立可 replay 的** —— 隔离不等于丢失。
父日志里只留两条记账：

```
#10 subagent/start  {"child_session": "ses_823f…", "preset": "explorer", "task": "…", "tools": [...]}
#11 subagent/end    {"child_session": "ses_823f…", "steps": 4, "child_messages": 8, …}
```

想追查"那次搜索到底看了什么"，按 `child_session` 打开子日志就行。

### 2. 受限的 action space

```python
def restricted(self, allowed: list[str]) -> "ToolRegistry":
    sub = ToolRegistry()
    for name in allowed:
        sub._tools[name] = self._tools[name]     # 共享 Tool 对象，不复制实现
    return sub
```

这是 s03 那句"registry 是行动空间的**唯一**来源"开始还债的时刻。

因为 `schemas()` 和 `get()` 来自同一个 dict，被过滤掉的工具会**同时**：

- 不出现在子 Agent 的 prompt 里（它根本不知道有这个工具）
- 执行时也找不到（就算模型硬猜出名字也调不动）

demo 直接验证给你看：

```
主 Agent：  bash, read, write, edit, glob, skill, grep, spawn_agent
explorer： read, glob, grep
被挡掉的： bash, write, edit, skill, spawn_agent

就算模型硬猜出 write 这个名字也没用：
  错误：没有名为 'write' 的工具。可用工具：read, glob, grep
```

> **两者一致才叫限制。只是不告诉它，那不叫限制。**

`explorer` 在结构上就改不了任何东西 —— 不需要靠"我们在 prompt 里叮嘱过它别改"。

### 3. SubagentPreset：Harness 划边界，模型选边界

```python
SUBAGENT_PRESETS = {
    "explorer": SubagentPreset(tools=["read", "glob", "grep"], identity="你是一个只读探索子 Agent…"),
    "editor":   SubagentPreset(tools=["read", "glob", "grep", "edit", "write", "bash"], …),
}
```

Harness 定义**能力信封**，模型决定**用哪个信封干什么活**。

反面写法：

```python
if "搜索" in task: spawn("explorer")     # ❌ Harness 在替模型判断
```

**为什么不干脆让模型自己指定工具列表？**

```python
spawn_agent(tools=["bash", "write"])     # ❌ 等于让模型给自己发权限
```

安全边界必须由 Harness 划，模型只能在已划好的边界里选一个。

### 4. 子 Agent 不继承父的上下文

```python
child_rt = RuntimeContext(cwd=WORKSPACE, tool_names=child_registry.names(),
                          project_notes=None, skill_catalog=[])
child_prompts = SystemPromptRegistry()
child_prompts.register(PromptSection("identity", 10, lambda c: preset.identity))
```

它是一个**新生的 Agent**，只知道任务本身。所以 `task` 参数的描述特意写了：

> "交给它的任务描述。**要写清楚，它看不到你的对话历史。**"

这是隔离的代价，也是它必须被明确告知的约束。

---

## 最小架构图

```
                   registry（8 个工具）
                         │
       ┌─────────────────┴──────────────────┐
       │                                    │ restricted(["read","glob","grep"])
       ▼                                    ▼
  主 Agent                            explorer 子 Agent
  ├─ Session（父）                     ├─ Session（子，独立文件）
  ├─ prompts（identity/skills/…）      ├─ prompts（只有子 identity）
  ├─ RuntimeContext（技能/项目/进度）    ├─ RuntimeContext（空）
  └─ Inbox                            └─ Inbox（只有 task）
       │                                    │
       │ spawn_agent(agent, task) ─────────▶│
       │                                    │  run_turn(...)
       │◀────── outcome.text（仅结论）───────┘
       │
       ├─ 父日志：subagent/start · subagent/end（log-only）
       └─ 主上下文：只多了 1 条 tool 结果
```

注意 `run_turn` **完全不知道"谁是子 Agent"** —— 它只是拿到了一个不同的
registry、一个不同的 prompt 注册表、一个不同的 session。

---

## 跑一下

```sh
python s09_subagent/code.py --demo
python s09_subagent/code.py --demo --debug
```

输出里的视觉边界很重要：

```
→ spawn_agent agent='explorer', task='…'
  ┌─ subagent[explorer] 启动  tools=read,glob,grep
    → grep pattern='def '
    → read path='auth/middleware.py'
    → read path='api/deps.py'
  └─ subagent[explorer] 结束  steps=4  子上下文 8 条消息 → 只返回 100 字符
  ✓ 鉴权集中在两处：auth/middleware.py:5 …
```

**框里的一切都不会进主上下文。** 主 Agent 只看到最后那个 `✓`。

---

## 为什么这样设计

### 为什么这不只是"省 token"

省钱当然是好处，但更重要的是**注意力**。

模型在 60000 token 的上下文里找那 3 行关键信息，和在 200 token 的上下文里
读一句结论，表现是不一样的。而且那 60000 token 会在**后面每一步**都参与注意力竞争 ——
你不是付了一次，是付了 N 次。

隔离让"探索的成本"和"结论的价值"解耦。

### 为什么子 Agent 要有独立的日志文件，而不是塞进父日志

如果塞进父日志（哪怕标记成 log-only），`derive_messages` 的逻辑就得
"跳过所有属于子会话的事件"。这个过滤条件会污染每一个日志消费者：
回放要跳、压缩要跳、审计要跳。

**一个独立的会话就是一个独立的日志。** 用 `child_session` 字段做引用，
而不是把两棵树塞进一个数组。

### 为什么返回值是纯文本，不是结构化数据

```python
return outcome.text or "（子 Agent 没有返回结论）"
```

因为它要进模型的上下文，而模型的上下文就是文本。

真实系统有更丰富的返回协议（子 Agent 可以调 `report` 工具提交结构化结论），
但那是优化，不是本质。本质是：**子 Agent 的产出必须是一个能装进一条消息的东西。**
如果它要返回 5000 行，那隔离就白做了。

所以 `explorer` 的 identity 里明确写着：

> "汇报里要给出具体的文件名和行号，**不要粘贴大段原文**"

### 为什么限制 spawn 深度

```python
MAX_SUBAGENT_DEPTH = 1   # 子 Agent 不能再 spawn
```

和 s02 的 `MAX_STEPS` 一样，这是**资源保护**，不是智能判断。
无限套娃会指数级烧钱。

s16 会放开这个限制（Agent Team 需要多层），但会换成更细的预算控制。

### 这算不算 Harness 替模型决策？

不算。检查一下分工：

| 谁 | 做什么 |
|---|---|
| Harness | 提供 `spawn_agent` 工具；定义 explorer/editor 两个信封；隔离上下文；限制工具 |
| 模型 | 决定**要不要**派、派**哪个**、交代**什么任务**、怎么用返回的结论 |

prompt 里的 `subagents` section 只描述**能力**，不写"什么时候该用哪个"：

```
- explorer：只读探索。适合大范围搜索、通读代码、定位问题，会返回一段结论。
- editor：可读写。适合把一个已经明确的改动落地并自验证。
```

"适合……"是在描述这个工具的性质，就像 `read` 的描述里写"读取文件内容"一样。
它不是 `if`。

---

## 与上一章相比发生了什么

| | s08 | s09 |
|---|---|---|
| 上下文 | 只有一个 | **主 / 子，物理隔离** |
| 大搜索的代价 | 永久占用主上下文 | **随子会话丢弃**（demo 省 97%） |
| 工具集 | 所有 Agent 一样 | `registry.restricted()` 按 preset 裁剪 |
| prompt | 全局一套 | 子 Agent 用自己的 `SystemPromptRegistry` |
| 新工具 | 7 个 | **8 个**（+`spawn_agent`） |
| 新事件 | — | `subagent/start` `subagent/end`（log-only） |
| `run_turn` | — | 多一个 `prompt_registry` 参数，**其余不变** |

`run_turn` 能被子 Agent 直接复用，是 s05–s07 把状态都推到参数里的回报。

---

## 真实系统里还有什么

- **多种 provider**：DeepSeek Harness 的 `ctx.subagents` 是一个**按名注册的
  provider 表**，同一个 `spawn` 接口背后可以是进程内子 Agent、fork 出的进程、
  甚至另一个产品（Codex / Claude Code）。我们只有"进程内 + 新 Session"一种。
- **可继续的子 Agent**：真实系统的子 Agent 可以保持存活，父 Agent 后续
  再给它发消息（`send_message` / `interrupt_agent`）。我们的是一次性的。
  s16 会做一个简化版的持续对话。
- **scope 与 shadowing**：真实系统里子 Agent 的注册（工具、prompt section、
  监听器）属于一个 **scope**，同名时"最近的 scope 赢"。这让"给某个 agent
  换一个 read 工具的变体"成为可能。我们用了最土的办法：直接建一个新注册表。
- **并发子 Agent**：多个子 Agent 同时跑。我们串行。
- **深度与血缘**：真实系统把 `parentSession` / `delegationDepth` 作为**数据**
  携带，而不是靠 scope 结构表达。我们的 `subagent/start` 里也记了 `parent`。

---

## 自己动手改

1. **给 explorer 再收紧一格**
   把它的工具改成 `["read", "grep"]`（去掉 glob），跑 demo，
   看它的 prompt 和执行同时变化。

2. **加一个 preset**
   ```python
   "tester": SubagentPreset(
       name="tester", description="只跑测试并汇报结果，不改代码。",
       tools=["read", "bash"], identity="你只负责运行测试并汇报失败信息，不要改任何文件。")
   ```
   **你没有改 `run_spawn` 一行代码。**

3. **量化隔离效果**
   在 demo 里把 `build_big_workspace` 的 `range(10)` 改成 `range(100)`，
   对比两组的字符数差距。

4. **故意让子 Agent 话痨**
   把 explorer 的 identity 改成"把你读到的所有内容原样汇报"，
   看主上下文的字符数怎么涨回去。这说明**隔离的效果取决于返回值的大小**，
   不是取决于有没有用子 Agent。

5. **读子会话日志**
   demo 会在临时工作区里生成 `*_sub_*.jsonl`，用 s05 的 `--replay` 打开它。
   （子会话是完整的、可独立回放的会话。）

6. **验证"限制"的一致性**
   ```python
   sub = registry.restricted(["read"])
   assert "write" not in [t["name"] for t in sub.schemas()]      # prompt 里没有
   assert sub.get("write") is None                               # 执行也拿不到
   ```

---

## 下一章

子 Agent 解决了"**新的**大块内容不要进主上下文"。但主上下文本身还是在长：

```
[step 1]  messages=1
[step 2]  messages=3
[step 3]  messages=5
...
[step 40] messages=79
```

一个跑了半小时的会话，上下文一定会撞上模型的窗口上限。

最直接的做法是截断：

```python
messages = messages[-20:]      # 只保留最近 20 条
```

**但这会立刻炸。** 因为第 20 条可能正好是一个 `tool_result`，
而它配对的 `tool_call` 在第 19 条 —— 被切掉了。模型侧直接报错：
"tool_result 找不到对应的 tool_use"。

而且截断意味着**遗忘**：模型不知道自己 20 步之前干了什么，会重复劳动。

有没有办法既缩短上下文，又不丢失历史、不切断配对？

而且，如果 s05 说"日志是唯一真相、messages 只是投影"，那压缩到底压的是**谁**？

→ [s10 — Context Compaction](../s10_context_compaction/)
