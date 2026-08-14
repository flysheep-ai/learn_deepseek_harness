# s16 — Agent Team

**English version: [README.en.md](README.en.md)**

[s15](../s15_capability_seams/) → **s16** → [s17](../s17_goal_loop/) → … → s18

> Harness 提供协作机制，**不替模型编写协作策略**。
>
> 这是全课程最需要警惕"替模型思考"的一章。

---

## 上一章留下的问题

s09 的子 Agent 是一次性的：`spawn` → 跑完 → 返回文本 → 丢弃。

现在用户给了一个更大的任务：

> "查一下这个 bug 的原因，写个修复，再让另一个人检查一下。"

这需要**多个 Agent 协作**。自然的做法是写死流程：

```python
if task_type == "research":    research_agent()     # ❌
elif task_type == "fix":       coding_agent()       # ❌
elif task_type == "review":    review_agent()       # ❌
```

**这是全课程最危险的一刻** —— 上面的代码是 Harness 在替模型决定
"创建谁、委派什么、什么时候收集结果"。

---

## 这一章解决什么

把协作拆成两部分，各归各家：

```
模型自己组织的协作                    Harness 提供的机制
┌─────────────────────────┐         ┌─────────────────────────┐
│ "先派 explorer 查原因    │         │ spawn_agent(role, task) │
│  再派 editor 改代码      │         │ send_message(agent, msg)│
│  让 reviewer 检查        │         │ receive()               │
│  最后自己合并结论"       │         │ list_agents()           │
└─────────────────────────┘         └─────────────────────────┘
  策略由模型决定                        机制只有四个动词
```

demo 里模型（由脚本假扮）的完整决策序列：

```
① spawn explorer（persistent）    ← 模型决定：先查
② receive                         ← 模型决定：收结论（触发 explorer 开工）
③ spawn editor（persistent）
④ send_message(editor, "explorer 说…请修复")
⑤ receive                         ← 收 editor 的产出
⑥ spawn reviewer（persistent）
⑦ send_message(reviewer, "editor 改了…请检查")
⑧ receive → list_agents → 收尾
```

**Harness 里没有任何一行代码写着这个顺序。** 它是模型在那个 turn 里一步步产生的。

---

## 新增的核心概念

### 1. 持续存活的成员（vs s09 的一次性 subagent）

```python
class MemberAgent:
    def __init__(self, name, preset, parent, tools, bus, tracer, provider_factory):
        self.inbox = Inbox()          # 别人可以随时发消息
        self.outbox: list[str] = []   # 产出排队等主 Agent 来收
        self.status = "idle"          # idle | working | done
```

四个差异：

| | s09 一次性 | s16 成员 |
|---|---|---|
| 生命周期 | spawn → 跑完 → 丢弃 | **持续存活，可反复对话** |
| 消息 | 只有初始 task | inbox 随时可投递 |
| 结果 | return 文本 | **outbox 排队，receive 收取** |
| 运行 | spawn 即跑 | **惰性：有消息才处理** |

### 2. 惰性运行：谁收到消息谁干活

```python
def run_once(self) -> None:
    if not self.inbox:
        return                      # 没消息就不动
    ...
    outcome = run_turn(..., session=self.session, inbox=self.inbox)
```

团队里谁在干活、什么时候干，由"谁收到了消息"决定。
没有中心调度器，没有工作流引擎 —— 这是协作机制的**最小实现**。

两个触发点：`send_message`（发出后顺手处理）和 `receive`（收取前先算账）。

### 3. receive 只交付**新**产出

```python
fresh = m.outbox[m.delivered:]
if fresh:
    m.delivered = len(m.outbox)
    parts.append(...)
```

第一版实现里 `receive` 每次都把全部 outbox 念一遍 ——
模型每收一次就重读一次旧结论，上下文被自己刷爆。

这是 s12 的 `notified` 标记的同一个坑，换了个位置又出现一次：
**轮询 + 事件混合结构里，"交付游标"是必需品。**

### 4. 预设角色变成注册表

```python
def register_preset(preset: SubagentPreset) -> None:
    SUBAGENT_PRESETS[preset.name] = preset
```

一个新角色（比如"安全审查员"）= 一次 `register_preset()`，
不改 SubagentPlugin 一行。角色的**定义**在 Harness 侧（能力信封），
**派谁干什么**仍在模型侧。

---

## 最小架构图

```
                    主 Agent
                      │
        ┌─────────────┼──────────────────┐
        │ spawn_agent │ send_message     │ receive
        ▼             ▼                  │
  ┌──────────┐  ┌──────────┐  ┌──────────┴───────┐
  │explorer  │  │ editor   │  │  reviewer        │
  │ inbox    │  │ inbox    │  │  inbox           │
  │ outbox   │  │ outbox   │  │  outbox          │
  │ (read,   │  │ (read,…  │  │ (read, glob,     │
  │  glob,   │  │  edit,   │  │  grep)           │
  │  grep)   │  │  write,  │  │  ← 受限工具集     │
  │          │  │  bash)   │  │                  │
  └──────────┘  └──────────┘  └──────────────────┘
   各自独立的 Session / Inbox / RuntimeContext（s09 的隔离原样保留）
   各自跑 run_turn —— 和主 Agent 是同一段循环代码
```

---

## 跑一下

```sh
python s16_agent_team/code.py --demo
python s16_agent_team/code.py --demo --debug
```

注意输出里的结构：

```
★ 团队成员 explorer 加入（persistent）
  → receive
    ┌─ agent[explorer] 开始处理 1 条消息
      → read path='core.py'
    └─ agent[explorer] 完成 steps=2 → outbox 57 字符
    ✓ 【explorer · done】
```

每个成员的行动都发生在**它自己的上下文**里（视觉上有框），
主上下文只看到 `receive` 返回的那段结论。

---

## 为什么这样设计

### 为什么成员复用 run_turn

```python
outcome = run_turn(self.provider_factory(), None, self.executor, self.tracer,
                   prompt_registry=self.prompts, rt=self.rt,
                   session=self.session, inbox=self.inbox)
```

成员**不是**另一套循环，它就是 `run_turn` 配上自己的 session/inbox/rt/prompts。

这正是 s05–s14 每一章都把状态推到参数里的回报：
一个循环，五种上下文（主 Agent、s09 一次性 subagent、团队成员、s17 的 goal、s18 的一切），
零份重复代码。

### 为什么"协作策略"这么容易被写死，以及怎么检查

写死的形式不止 `if task_type` 一种，它们都长一个样：

```python
if len(unread_replies) >= 2:                      # ❌ 替模型决定什么时候看消息
    collect_all_results()
for member in members.values():                   # ❌ 替模型决定全都要干活
    member.run()
if reviewer_found_bugs:                           # ❌ 替模型决定下一步
    send_back_to_editor()
```

检查方法很简单：

> **把模型抽掉，这段逻辑还能完成协作吗？**
> 能 → 是 Harness 机制（spawn / send / receive / status 都是）。
> 不能，它的存在意义就是替模型产生决策 → 是越界。

demo 第 4 部分把两边逐条列出来了。

### 为什么团队状态不是新的 session 事件

s16 **没有新增任何事件类型**。

成员的存在与产出，是主会话里的工具结果（`已创建团队成员 editor…`）；
成员的状态，在他们自己的事件日志里（`turn/start` … `assistant/message`）。

真实 Harness 会为"agent 注册表"提供查询服务（`ctx.agents`），
但持久化仍然走 session 日志。**状态要么可推导，要么已存在，
就不该发明第二份存储。**

### 为什么成员是"按角色一个"，而不是无限个

```python
if agent in members:
    return f"错误：角色 {agent} 已经有一名成员了"
```

s09 的 MAX_SUBAGENT_DEPTH 是防套娃；这里的"一角色一成员"是另一种简化：
团队成员集合保持"一只手数得过来"的规模，模型才能有效协作。

真实系统允许任意数量的 agent 实例（带 id 寻址）。我们留了名字作为地址，
但没有放开数量 —— 这是复杂度控制，不是能力上限。

---

## 与上一章相比发生了什么

| | s09（当时的 subagent） | s16 |
|---|---|---|
| 生命周期 | 一次性 | **持续存活，可反复对话** |
| 消息方向 | 只有父 → 子（初始 task） | **双向**（send / receive） |
| 结果路径 | return 文本 | **outbox + 交付游标** |
| 运行时机 | spawn 即跑 | **惰性（有消息才处理）** |
| 预设 | 硬编码 dict | **可注册** |
| 协作 | 模型只能一次 spawn | **模型自己编排整个团队** |
| 新事件 | — | 零（复用 tool 结果 + 成员自己的日志） |

---

## 真实系统里还有什么

- **agent 注册表**：dsh 的 `ctx.agents` 是 live registry，
  `list_agents` / `send_message` / `interrupt_agent` 是它的控制工具。
  我们只有 `h.services["agents"]` 一个 dict。
- **多 provider**：真实系统的 subagent 是 seam（s15 那套），
  同一 spawn 接口背后可以是 in-process / fork / ACP / 别的产品。
- **血缘**：`parentSession` / `delegationDepth` 作为**数据**携带，
  跨进程也能追踪。我们的 `session/start` 里记了 `parent`。
- **中断**：`interrupt_agent` 能打断一个正在跑的成员。
- **持续会话的续聊**：成员结束后，其 session 可以保存并在下次会话续上。
  我们每次 demo 都是新建的。

---

## 自己动手改

1. **加一个角色**
   ```python
   register_preset(SubagentPreset(
       "tester", "只跑测试并汇报结果，不改代码。",
       ["read", "bash"],
       "你只负责运行测试并汇报失败信息，不要改任何文件。"))
   ```
   **你没有改 SubagentPlugin 一行代码。**

2. **把 receive 改回"全量返回"**
   删掉 `delivered` 游标，看模型每收一次就重读一次旧结论。

3. **让成员每次收到消息都跑，而不是惰性**
   去掉 `if not self.inbox: return`，看空转的成员怎么浪费模型调用。

4. **检查你的代码里有没有越界**
   搜索你自己的 demo：有没有 `if "修复" in task` 之类的话？
   有的话，把它换成"把信息给模型，让模型决定"。

5. **写一个"团队纪要"插件**
   挂在 `EVT_TURN_END` 上，把团队状态快照写进日志。
   注意：这必须是**观察**（emit），不能改变任何行为。

---

## 下一章

现在 Agent 有了团队，能跑很长的任务了。但：

```python
run_turn(...)          # 跑完了
# 目标完成了吗？ —— 没人知道，也不重要。turn 结束 = 结束。
```

用户的目标"修好这个 bug"往往不是一个 turn 能做完的：

- 模型改完代码，测试挂了 → 需要**再来一轮**
- 模型卡住了（依赖缺失、权限不够）→ 需要有人知道"卡在哪"
- 用户关掉终端去吃饭 → 目标应该**活着**，下次开机继续

而现在这一切都不存在。目标只存在于模型的上下文里，
turn 结束就是真的结束，没有人会再叫它起来。

能不能让目标成为 Harness 的一份**持久状态**：

- 有明确的生命周期（进行中 / 阻塞 / 完成）
- 有预算（最多自动继续几轮）
- 有关掉终端也不丢的 checkpoint

然后让 Harness 问自己一个问题：

> **这一轮结束的时候：目标完成了吗？该不该再来一轮？**

→ [s17 — Goal Loop](../s17_goal_loop/)
