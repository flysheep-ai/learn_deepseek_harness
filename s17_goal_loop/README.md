# s17 — Goal Loop

**English version: [README.en.md](README.en.md)**

[s16](../s16_agent_team/) → **s17** → [s18](../s18_full_harness/)

> "修好这个 bug" 这种目标，应该住在哪里？
>
> 如果只住在模型的上下文里，turn 结束就是真的结束。

---

## 上一章留下的问题

s16 的 Agent 有了团队，能跑很长的任务。但：

```python
run_turn(...)          # 跑完了
# 目标完成了吗？—— 没人知道，也不重要。turn 结束 = 结束。
```

用户的目标"修好这个 bug"往往不是一个 turn 能做完的：

- 模型改完代码，测试挂了 → 需要**再来一轮**
- 模型卡住了（依赖缺失、权限不够）→ 需要有人知道"卡在哪"
- 用户关掉终端去吃饭 → 目标应该**活着**，下次开机继续

而现在这一切都不存在。目标只存在于模型的上下文里：

- 没有任何东西在 turn 结束时问一句"做完了吗"
- 没有任何预算约束 —— 模型可以无限烧钱空转
- 进程一关，目标就死

---

## 这一章解决什么

让目标成为 Harness 的一份**持久状态**，并在 turn 循环外面套一层评估循环：

```
Goal（持久状态，在 Harness 手上）
┌───────────────────────────────────────────┐
│ statement: "修好 divide 的除零问题"        │
│ status:    active / paused / blocked /    │
│            complete                       │
│ round: 2/4 ← 预算                          │
└───────────────┬───────────────────────────┘
                │ 每个 turn 结束时
                ▼
     evaluate：目标达成了吗？
      ├─ complete → 收工（记 goal/complete）
      ├─ blocked  → 停下来，记原因（不烧钱）
      └─ continue → 注入"[目标未完成] 继续"，开新一轮
                    （round 用尽 → blocked: budget）
```

demo 展示了完整的三轮生命周期（第 1 轮没动手 → 第 2 轮改出语法错误 → 第 3 轮修复验证）：

```
↻ goal continue（第 1/4 轮）：模型这轮只是在读文件，没有改代码。
↻ goal continue（第 2/4 轮）：模型改了一半（加了保护但语法错了），需要重跑验证。
● goal complete：divide 已有除零保护且验证通过。
```

---

## 新增的核心概念

### 1. Goal 是状态，不是一段对话

```python
@dataclass(frozen=True)
class Goal:
    statement: str
    status: str = "active"      # active | paused | blocked | complete
    round: int = 0              # 已经自动继续了几轮
    max_rounds: int = 5         # 预算
    reason: str = ""            # blocked/complete 的原因
```

和 s11 的 Task 是不同维度：

```
Goal   用户要的**结果**  —— 有预算、有生命周期、被评估
Task   模型拆的**步骤**  —— 跨 turn 的进度清单
```

一个 Goal 通常产生多个 Task；评估 Goal 时看的不是 Task 清单，
而是"用户要的结果达成了没有"。

### 2. 四种状态 + 预算

```
active    正在推进
paused    人暂停了它（本课程未演示，状态存在即可）
blocked   卡住了 / 预算用尽 —— 停止自动继续，等人来
complete  评估器判定达成
```

预算（`max_rounds`）的意义：

> 它是**资源保护**，不是**智能判断**。
> Harness 不知道任务难不难，它只保证"自动继续"有个上限。
> 上限到了就 `blocked: budget`，等**人**来决定要不要再续。

这和 s02 的 MAX_STEPS、s09 的 MAX_SUBAGENT_DEPTH 是同一族设计。
到这一章，三级预算终于齐了：

```
round  目标层面   max_rounds（s17）
turn   输入层面   MAX_STEPS_PER_TURN（s06）
step   调用层面   （工具自身超时）
```

### 3. 评估是模型调用，不是 if

```python
reply = self.evaluator.chat(
    [{"role": "user", "content": f"目标：{goal.statement}\n\n最近的工作记录：\n{...}"}],
    system=EVALUATE_SYSTEM)
```

"目标达成了吗"这个问题**不是 Harness 写几个 if 能回答的** ——
它需要理解任务的语义。所以把它交给模型（评估器）。

Harness 只做**生命周期规则**：

```python
if verdict == "done":      → goal/complete，不再打扰模型
elif verdict == "blocked": → goal/blocked（带原因），不再烧钱
else:                      → round+1，注入"继续"，开新一轮
```

而且评估器的输出解析是**保守**的：

```python
# 格式错时宁可当作 continue（再试一轮），也不要误判 done 提前收工。
```

评估器挂了 → 当作 blocked（不假装成功，也不无限烧钱）。

### 4. 一切都是事件，进程重启目标还活着

```
#18 goal/start     round=0
#34 goal/evaluate  round=0  verdict=continue
#35 goal/start     round=1
#51 goal/evaluate  round=1  verdict=continue
#52 goal/start     round=2
#82 goal/evaluate  round=2  verdict=done
#83 goal/complete
```

`GoalStore` 和 `TaskStore` 是同一个模式：**从事件日志派生，不另存**。

关掉终端 → `Session.load` → `GoalStore.current()` → 目标还在，
带着它的轮次、预算和状态。

这就是"checkpoint"的最小形态：**日志本身就是 checkpoint。**

---

## 最小架构图

```
   用户设定目标
        │
        ▼
   goal/start（active, round=0）
        │
   ┌────────────────────────────────────────┐
   │              Goal Loop                 │
   │                                        │
   │   ┌─ run_turn（s06 的循环，没变）─┐     │
   │   │  做 → 观察 → …                 │     │
   │   └──────────┬─────────────────────┘     │
   │              │ turn 结束                 │
   │              ▼                           │
   │   EVT_TURN_END → GoalPlugin 监听器       │
   │              │                           │
   │              ▼                           │
   │   评估器（模型）：done / blocked / continue│
   │        │        │              │         │
   │        ▼        ▼              ▼         │
   │   complete   blocked      round < max?   │
   │    收工      停止烧钱      ├─ 是 → round+1│
   │                           │   注入"继续" │
   │                           └─ 否 → blocked│
   │                                        │
   └────────────────────────────────────────┘
```

注意：**run_turn 一行没改**。goal 机制整个挂在 s13 的 `EVT_TURN_END`
观察者上，是 s13/s14 架构的直接受益者。

---

## 跑一下

```sh
python s17_goal_loop/code.py --demo
python s17_goal_loop/code.py --demo --debug
```

真实模型下：

```sh
python s17_goal_loop/code.py
> /goal 把这个项目里所有 TODO 注释解决掉
> 开始吧
# 之后每一轮结束，评估器会判定 continue / blocked / done
> /goal        # 随时查看状态
```

---

## 为什么这样设计

### 为什么不写 `while not done:`

反面写法长这样：

```python
while not done:
    run_turn(...)
    if "测试通过" in last_text:        # ❌ 替模型判断目标是否达成
        done = True
    elif error_count > 3:              # ❌ 替模型判断"卡住了"
        done = True
    elif steps > 100:                  # ✅ 这个可以，是预算
        done = True
```

前两个 if 是 Harness 在**读任务内容做判断** —— 这是越界。

正确的分工：

```
Harness 管：目标保存、状态保存、预算、停止条件、checkpoint
模型管：下一步做什么、是否修改计划、如何解决问题
```

评估器（一个模型调用）负责"达成 / 卡住 / 继续"的语义判断 ——
它把"判断"这个职责放在了它唯一能胜任的地方：模型的输出里。

### 为什么评估器输出解析要保守

```python
v = word if word in ("done", "blocked", "continue") else "continue"
```

两个方向的误判代价是不对称的：

- 把 continue 误判成 done → **目标没完成就收工了**，用户回来发现活没干完
- 把 done 误判成 continue → 多烧一轮钱，下一轮评估器还有机会纠正

所以失败方向选"继续"。**误判的代价不对称时，往代价小的方向偏。**

### 为什么 goal section 在 active 时才出现

```python
@ctx.section("goal", 14)
def _goal_section(rt) -> str | None:
    if g is None or g.status != "active":
        return None
```

complete 之后这段就从 prompt 里消失了（demo 第 6 部分验证：0 字符）。

目标已经完成，它不该再占用模型的注意力 —— 这是 s07 就讲过的
"prompt 是运行时产物"的自然延伸：**goal 状态变了，prompt 跟着变。**

### 评估器的输入只有"最近的工作"

```python
msgs = derive_messages(ctx.session)[-24:]      # 只看最近的工作
```

评估不需要从头看到尾。这和 s10 的压缩是同一个方向：
**长会话的每个消费者都在按需裁剪自己的视图。**
模型看全量，评估器看尾部，压缩器看边界。

---

## 与上一章相比发生了什么

| | s16 | s17 |
|---|---|---|
| turn 结束之后 | 就是结束 | **有东西在评估、在续跑** |
| 目标的位置 | 模型的上下文里 | **Harness 的持久状态** |
| 进程重启 | 目标丢失 | **goal 从事件日志恢复** |
| 预算 | 只有 turn 步数上限 | **round 层面有了 max_rounds** |
| 新对象 | — | `Goal` / `GoalStore` / `GoalPlugin` |
| 新事件 | — | `goal/start` `/evaluate` `/blocked` `/complete`（log-only） |
| run_turn | — | **一行没改**（挂在 EVT_TURN_END 上） |

---

## 真实系统里还有什么

- **activation（激活）是进程级的**：DeepSeek Harness 里 goal 的状态是持久的，
  但"允许自动继续"是一个进程内权限（armed/disarmed）—— 恢复会话后必须
  人重新授权，自动化才不会在无人值守时自己跑起来。这是一个安全设计，
  我们简化为"active 就自动继续"。
- **goal round 是特殊来源的 turn**：同一会话里，人的 turn 不消耗 goal 预算，
  只有 goal 驱动的续跑轮才计数。我们简化了。
- **paused / 中断**：人可以暂停目标，暂停期间 turn 照常，只是不再评估。
- **Ralph loop**：dsh 里还有一种"每个 round 开一个**全新子会话**"的
  工作流（Ralph），它和 same-session goal 是不同的策略。我们只教了后者。
- **blocked 的策略代码**：`reason` 在真实系统里带机器可路由的 code
  （如 `budget-exhausted`），我们只有人话文本。

---

## 自己动手改

1. **把评估器脚本改成永远 continue**
   观察 `max_rounds` 用尽后 goal 变成 `blocked: 预算用尽`，
   且 Harness 停止注入"继续" —— **烧钱有上限**。

2. **给评估器一个错误的输出格式**
   `scripted("我觉得差不多了")` —— 观察它被当作 continue 处理。

3. **把 goal 换成内存变量，丢掉事件**
   把 `GoalStore` 改成 `self._goal = ...`，然后重启进程。
   体会 s05 那句"凡是要活过进程的东西，都得能从事物日志重建"。

4. **加一个 `/goal pause`**
   设置 `paused` 状态，让 turn-end 监听器跳过评估。
   （提示：`GoalStore.update` 已经支持任意状态。）

5. **让评估器读全量上下文**
   把 `[-24:]` 去掉，对比评估质量（真模型下）和每次评估的 token 成本。

---

## 下一章

从 s01 到 s17，每一个机制都已经单独立起来了：

```
Agent Loop → Tool → Registry → 权限 → 事件日志 → Turn/Step
→ Prompt 组装 → 技能 → 子 Agent → 压缩 → 任务 → 后台任务
→ 事件总线 → 插件 → Capability → 团队 → Goal
```

现在要做最后一步：**把它们组装成一台完整的机器**，然后用一个
真实任务来验收 ——

> "帮我检查这个项目为什么测试失败，并修复它。"

Harness 从头到尾不知道这是一个 debugging task。
它只是把 tools / context / state / permission / session 摆好，
然后看着模型自己走完全程。

→ [s18 — Full Harness](../s18_full_harness/)
