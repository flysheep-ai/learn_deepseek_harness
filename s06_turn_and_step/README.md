# s06 — Turn and Step

**English version: [README.en.md](README.en.md)**

[s05](../s05_session_event_log/) → **s06** → [s07](../s07_prompt_assembly/) → … → s18

> 一次用户输入，等于一次模型调用吗？
>
> 不等于。而这个区别不显式表达出来，预算、中断、"轮结束时做点什么"就全都无处安放。

---

## 上一章留下的问题

s05 的日志有 19 条事件，但它是**平的**：

```
# 3 assistant/message   (请求 1 个工具)
# 7 tool/result
# 8 assistant/message   (请求 1 个工具)
#12 tool/result
```

**哪些事件属于同一次模型调用？哪些属于同一次用户输入？**

日志答不出来。于是三类需求全部落空：

| 想做的事 | 为什么做不到 |
|---|---|
| "每次用户输入最多 20 步"的预算 | "步"没有边界，数不出来 |
| "这一轮结束时自动 git commit" | 没有"轮结束"这个时刻 |
| 用户中途插一句话 | 它属于当前这一轮还是下一轮？无从定义 |

s02–s05 里那个 `for _ in range(MAX_STEPS)` 一直在假装解决第一个问题，
但它只是一个循环计数器，日志里查不到，恢复会话后也归零。

---

## 这一章解决什么

引入两级结构，并把它们写进日志：

```
Turn  一次输入的**排空**（drain）。包含**零个或多个** Step。
Step  一次模型请求 + 它引发的工具执行。
```

以及一个配套设施：**Inbox**（输入先排队，由 step 认领）。

---

## 新增的核心概念

### 1. Step = 一次模型请求 + 它引发的工具执行

注意"引发的工具执行"是 step 的一部分，不是下一个 step。
一次模型回复请求了 3 个工具，那 3 次执行都属于**这一个** step。

### 2. Turn = 一次输入的排空

turn 在认领第一批输入之前开启，在"什么都不欠了"之后关闭。

```python
if reply.wants_tools:
    pass                      # 工具结果欠模型一次请求 → 继续
elif inbox:
    pass                      # 用户中途插话 → 同一轮里继续
else:
    reason = "natural-stop"   # 什么都不欠了 → 收工
    break
```

两个继续条件都**不在判断任务内容**。Harness 仍然不知道这是 debug 任务还是写文档任务。

### 3. 零 step 的 turn 是合法的

```
Turn 3
  └── Turn 3 end  reason=no-input steps=0
```

turn 开了，但没有输入可认领 / 输入被过滤空了 / 被取消了 —— 得到一个没有 step 的 turn。

这不是边角情况，而是**必须记录的事实**：有一次尝试发生过，但它没进模型。
如果 turn 和 step 是同一个概念，这条记录根本无处安放。

### 4. Inbox：输入先排队，由 step 认领

```python
inbox.put("等等，顺便看看 main 函数还在不在", source="steering")
```

s05 之前，用户输入是**立刻**变成 `user/message` 的。一旦 turn 可以跨多个 step，问题就来了：

> 用户在模型跑到第 3 步时插了一句话，这句话属于哪一轮？

答案：进 inbox 排队，由**下一个 step 认领**（claim）。它属于当前 turn，
会立刻影响模型的下一次请求，而不用等这一轮结束。

demo 的 Turn 2 演示了这件事：

```
Turn 2
  ├── Step 1
  │     user(user)       再检查一下 config.py
  │     tool call        read
  ├── Step 2
  │     user(steering)   等等，顺便看看 app.py 里 main 函数还在不在   ← 插进来了
  │     tool call        grep
  ├── Step 3
  │     model            config.py 里 DEBUG=True…；main 函数还在。
  └── Turn 2 end  reason=natural-stop steps=3
```

**一个 turn，两条用户消息，三个 step。**

`source` 字段区分了输入的来源（`user` / `steering` / `injected`）。
s08 的技能内容、s09 的子 Agent 结果、s12 的后台任务完成通知，
都会从这条同样的路径进来 —— 来源不同，认领机制只有一套。

### 5. Tracer：把内部发生的事情显示出来

```sh
python s06_turn_and_step/code.py --demo --debug
```

```
[turn 1 start]
  [step 1]  claimed=1 (user)
    → model request   messages=1 tools=6 system=139chars
    ← model reply     text=0chars tool_calls=1 [read] usage=39/4
    · tool pre        read path='app.py' → allow
    · tool result     read ok 77B
  [step 1 end]
  [step 2]  claimed=0
    → model request   messages=3 tools=6 system=139chars
    ← model reply     text=0chars tool_calls=1 [edit] usage=59/15
    · tool pre        edit path='app.py', old_text='0.1.0', … → ask→y
    · tool result     edit ok 10B
  [step 2 end]
  [step 3]  claimed=0
    → model request   messages=5 tools=6 system=139chars
    ← model reply     text=14chars tool_calls=0 [-] usage=61/3
  [step 3 end]
[turn 1 end] reason=natural-stop steps=3
```

盯住 `messages=1 → 3 → 5`：**上下文在每一步增长 2 条**（assistant + tool）。
这就是 s10 要处理的那个问题的来源。

Harness 是抽象系统，看不见就学不会。从这一章起每章都有 `--debug`。

---

## 最小架构图

```
   用户输入 ──▶ Inbox ─────┐
   注入上下文 ─▶ Inbox ─────┤
                          │  claim()
   ┌──────────────────────▼──────────────────────┐
   │  turn/start                                 │
   │    ┌─ step/start ─────────────────────┐     │
   │    │   claim → user/message           │     │
   │    │   derive_messages()              │     │
   │    │   model request                  │     │
   │    │   assistant/message              │     │
   │    │   tool/call → 执行 → tool/result  │     │
   │    └─ step/end ───────────────────────┘     │
   │           │                                 │
   │           ├─ 模型还要工具？   ──▶ 下一个 step  │
   │           ├─ inbox 有新输入？ ──▶ 下一个 step  │
   │           └─ 都没有          ──▶ 收工         │
   │  turn/end  reason=…                         │
   └─────────────────────────────────────────────┘
```

---

## 跑一下

```sh
python s06_turn_and_step/code.py --demo
python s06_turn_and_step/code.py --demo --debug
```

结尾的统计是这一章的总结：

```
3 条用户消息 → 3 个 turn → 6 个 step。三个数字互不相等。
```

---

## 为什么这样设计

### 为什么 turn 号要从日志里读，不用内存计数器

```python
def last_turn(self) -> int:
    return max((e.data["turn"] for e in self._events if e.type == EV_TURN_START), default=0)
```

内存计数器是**第二份真相**。恢复会话时它归零，于是新事件的 turn 号
会和历史撞车，整条日志就废了。

这是 s05 那条规则的直接推论：turn 号既然是事实的一部分，就必须从事实推导。
**凡是能从日志算出来的，就不要另存一份。**

### 为什么 user/message 要等到被认领时才写日志

s05：输入进来立刻 `append(user/message)`。
s06：输入进 inbox，**被 step 认领时**才 append。

差别在于日志里 `user/message` 的**位置**。现在它精确表达了"模型在第几步看到这条消息"。
如果在入队时就写，插话消息会出现在 Turn 2 Step 1 之前 ——
可日志会显示模型在 Step 1 就看到它了，而实际上没有。**日志会说谎。**

### 为什么 turn/step 事件是 log-only

turn / step 是 **Harness 的结构**，不是给模型看的内容。
模型只关心消息序列，不关心它被怎么分组。

投影时它们被跳过，但它们让日志从"平的"变成"有层次的"：
`print_turn_tree()` 能画出那棵树，而 s05 的日志画不出来。

### 为什么 MAX_STEPS 是 per-turn 的

```python
MAX_STEPS_PER_TURN = 12
```

s02–s05 那个 `MAX_STEPS` 是每次 `agent_loop` 调用的上限，
换个说法就是"每次用户输入的上限" —— 只是当时没有词汇把它说清楚。

现在有了：它是 **turn 预算**。而 s17 的 Goal 会引入更外一层的 **round 预算**
（"这个目标最多允许自动继续 5 轮"）。

三级预算对应三级结构：

```
round  一次外层策略迭代（目标继续了一轮）
turn   一次输入的排空
step   一次模型请求
```

没有这套词汇，"限制 Agent 跑多久"这句话就是含混的。

---

## 与上一章相比发生了什么

| | s05 | s06 |
|---|---|---|
| 日志结构 | 平的 | **turn/step 分层** |
| 新事件 | — | `turn/start` `turn/end` `step/start` `step/end` |
| 用户输入 | 立刻进日志 | **进 Inbox 排队，被 step 认领** |
| 中途插话 | 无从定义 | 进入**当前 turn** 的下一个 step |
| 步数上限 | 循环计数器 | turn 预算，记录在 `turn/end.reason` |
| 结束原因 | 丢失 | `natural-stop` / `max-steps` / `no-input` |
| 可视化 | 事件列表 | **turn/step 树** + `--debug` trace |
| 入口函数 | `agent_loop(...)` | `run_turn(...)` |

---

## 真实系统里还有什么

- **`agent/pre-step` 拦截**：真实 Harness 在进入 step 之前有一个 waterfall，
  监听器可以**改写**认领到的消息，或者直接**拒绝**这一批。
  被拒绝时会得到一个 0 step 的 turn —— 这正是我们 `reason="no-input"` 的工业版本。
  s13 会用 EventBus 实现它。
- **`turn-stopping` 检查点**：turn 自然结束前的一个串行检查点，
  s17 的目标继续就挂在这里（"目标还没完成？再开一轮"）。
- **取消**：真实系统把 abort signal 一路传到工具体内，用户 Ctrl-C 能立刻中断，
  日志留下 `reason="cancelled"`。我们只有硬上限。
- **inbox 的唤醒语义**：有些消息立刻唤醒 driver（用户提问），
  有些则**静静排队**直到别的消息唤醒它（注入的上下文）。
  我们的 inbox 全部同等对待。

---

## 自己动手改

1. **让 turn 跑到预算上限**
   把 `MAX_STEPS_PER_TURN` 改成 2，重跑 demo。
   看 `turn/end` 的 `reason` 变成 `max-steps`。

2. **多插几句话**
   在 `steer_after_first_read` 里连着 `inbox.put()` 三次。
   观察它们在**同一个 step** 里被一起认领（`claimed=3`）。

3. **实现 turn 结束钩子**
   在 `session.append(EV_TURN_END, ...)` 之前加一句
   `if reason == "natural-stop": print("[hook] 该 git commit 了")`。
   注意：**这个位置在 s05 是不存在的**，因为没有"轮结束"这个时刻。

4. **统计每个 turn 花了多少 token**
   遍历日志，按 `turn` 字段把 `request/usage` 事件分组求和。
   （这是 s10 上下文压缩的前置数据。）

5. **在 --debug 下数上下文增长**
   看 `messages=1 → 3 → 5 → …`。算一下：一个 20 步的 turn，
   上下文会有多少条消息？如果每条 tool 结果 2000 token 呢？

---

## 下一章

现在看一眼 `--debug` 输出里这个数字：

```
→ model request   messages=1 tools=6 system=139chars
```

`system=139chars`。它来自：

```python
def make_system(cwd, reg):
    return (f"你是一个编程 Agent，工作目录是 {cwd}。\n"
            f"可用工具：{', '.join(reg.names())}。\n直接动手，不要解释。")
```

这个函数马上要出事了。后面的章节要往 system prompt 里塞：

- 可用技能清单（s08）
- 当前任务列表（s11）
- 后台任务状态（s12）
- 子 Agent 能力说明（s09）
- 项目约定（AGENTS.md / CLAUDE.md）

如果继续往这个 f-string 里拼，它会变成一个 200 行、谁也不敢改的巨型字符串：
加一个技能要改它、加一个工具要改它、加一个任务要改它。
而且这些内容**不是每次请求都需要**——有些只在某些条件下才该出现。

system prompt 到底是一个常量，还是一个**每次请求现场组装出来的运行时产物**？

→ [s07 — Prompt Assembly](../s07_prompt_assembly/)
