# s11 — Task System

**English version: [README.en.md](README.en.md)**

[s10](../s10_context_compaction/) → **s11** → [s12](../s12_background_jobs/) → … → s18

> 计划应该放在模型脑子里，还是放在 Harness 手上？

---

## 上一章留下的问题

给 Agent 一个大任务：

> "把这个项目里所有 print 换成 logging，然后跑测试确认没坏。"

模型说："好的，我分三步：① 替换 core.py ② 替换 cli.py ③ 跑测试。"

然后它开始干第一步。20 个 step 之后，s10 的压缩触发了两次。

**它忘了第 3 步。**

因为"我打算做三件事"这句话只存在于**模型说过的一段 assistant 消息**里。
它是上下文的一部分，会被压缩遮蔽、会被后续内容稀释。

s10 的摘要提示词里写了"保留还没做完的事" —— 但那是在**祈祷**模型每次都写对，
不是**保证**。

---

## 这一章解决什么

把计划从"模型说过的一段话"变成"Harness 持有的一份状态"：

```
模型脑子里的计划                 Harness 手上的状态
┌───────────────────┐          ┌──────────────────────────┐
│ "我分三步：        │          │ ● [t1] 替换 core.py       │
│  ① 替换 core.py    │  ✗ 会被  │ ● [t2] 替换 cli.py        │
│  ② 替换 cli.py     │   压缩   │ ◐ [t3] 跑测试 ← 依赖 t1,t2│
│  ③ 跑测试"         │   遮蔽   │                          │
└───────────────────┘          └──────────────────────────┘
   一条 assistant 消息            每一步重新渲染进 prompt
```

demo 的实证：

```
这一轮压缩了 3 次，遮蔽了 15 条消息
但 task/write 是 log-only，压缩碰不到它：
  日志里有 4 条 task/write 快照，全部完好
  当前清单从最后一条快照（#87）重新渲染
```

---

## 新增的核心概念

### 1. Task：字段刻意少

```python
@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str = "pending"          # pending / in_progress / completed / failed
    depends_on: tuple[str, ...] = ()
    note: str = ""
```

没有 priority、没有 assignee、没有 estimate、没有 due date。

**每多一个字段，模型每步都要多读一遍。** 那些是项目管理软件的字段，
不是 Agent 需要的。

### 2. 整表快照（`task/write`），不是增删改事件

```python
def write(self, tasks: list[Task]) -> None:
    self.session.append(EV_TASK_WRITE, {"tasks": [t.to_json() for t in tasks]})

def current(self) -> list[Task]:
    snapshot = []
    for ev in self.session.events():
        if ev.type == EV_TASK_WRITE:
            snapshot = ev.data["tasks"]      # 后写覆盖先写
    return [...]
```

为什么不做 `task/created` + `task/updated` + `task/deleted`？

因为整表快照的重放规则只有一句话：**取最后一条**。

细粒度事件要维护"更新了一个不存在的 id 怎么办"、"删除后又更新怎么办"
这类边角，而这些复杂度换不来任何东西 —— 任务清单本来就小，整表写一次也不贵。

工具的描述里也把这件事说清楚了：

> "完整的任务列表。这是**覆盖写，不是追加** —— 没列出来的任务会消失。"

### 3. `task/write` 是 log-only —— 这是整章的关键

```python
SURFACE_EVENTS = {EV_USER_MESSAGE, EV_ASSISTANT_MESSAGE, EV_TOOL_RESULT}
# EV_TASK_WRITE 不在里面
```

因为它不是消息，所以：

- 它**不参与投影** → s10 的压缩碰不到它
- 它**每一步重新渲染**进 prompt（通过 `tasks` section）

这就是"把计划从模型脑子里搬到 Harness 手上"的全部技术含义。

清单在 prompt 里排得很靠前（`order=15`）：它是"当前在干什么"的锚点，
应该在环境、工具这些背景信息之前被读到。

### 4. Harness 校验状态自洽，但不决定清单内容

这条边界是本章最需要拿捏的地方：

| 谁管 | 什么 |
|---|---|
| **Harness** | id 重复 / 依赖了不存在的 id / 依赖成环 / 前置未完成就标 completed |
| **模型** | 清单里该有哪些任务、怎么拆、先做哪个、什么时候重新规划 |

demo 里故意演示了一次校验失败：

```
✗ 错误：任务 t3 标成了 completed，但它依赖的 t1, t2 还没 completed。
       清单未更新，请修正后重新提交完整清单。
```

注意两点：

- **校验失败不写入**。宁可让模型重试一次，也不能让状态坏掉。
- 错误信息**可行动**：说清楚哪个任务、依赖谁、该怎么办。

对比一下越界的写法：

```python
if len(tasks) > 5:
    return "错误：任务太多了，请合并"        # ❌ 这是在替模型规划
if not any(t.status == "in_progress" for t in tasks):
    tasks[0].status = "in_progress"          # ❌ 这是在替模型决定先做哪个
```

---

## 最小架构图

```
   模型调用 task_write(tasks=[...])
        │
        ▼
   TaskStore.validate()  ← id 重复？依赖不存在？成环？前置未完成？
        │
        ├─ 不通过 ──▶ 返回可行动的错误，**不写入**
        │
        └─ 通过
             │
             ▼
        session.append("task/write", {tasks})   ← log-only 整表快照
             │
             │  （每个 step 开始时）
             ▼
        TaskStore.current()  ← 取最后一条快照
             │
             ▼
        RuntimeContext.tasks
             │
             ▼
        PromptSection("tasks", order=15)  ──▶ system prompt
                                              ▲
                       s10 的压缩够不到这里 ────┘
```

---

## 跑一下

```sh
python s11_task_system/code.py --demo
python s11_task_system/code.py --demo --debug
```

输出里的关键三段：

```
→ task_write  ✓ 任务清单已更新：3 项，已完成 0 项。
→ task_write  ✓ 错误：任务 t3 标成了 completed，但它依赖的 t1, t2 还没 completed。
→ read core.py
⟲ 上下文压缩：5 条消息 → 1 条摘要  664 → 467 tokens      ← 压缩发生了
→ edit core.py
→ task_write  ✓ 任务清单已更新：3 项，已完成 1 项。
...
任务清单（Harness 状态，不在模型脑子里）
  ● [t1] 把 core.py 的 print 换成 logging  // load() 已改
  ● [t2] 把 cli.py 的 print 换成 logging
  ● [t3] 跑测试确认没坏  ← 依赖 t1, t2  // smoke ok
```

**三次压缩之后，三项任务一项没丢。**

---

## 为什么这样设计

### 为什么不缓存 `TaskStore.current()`

```python
if TASKS is not None and prompt_registry is None:
    rt.tasks = TASKS.current()        # 每步重新读
```

理由和 s05 每步重新 `derive_messages` 完全一样：

> **缓存就是第二份真相，迟早和日志对不上。**

任务清单只有几项，每步遍历一遍日志的开销可以忽略。
（真实系统会加带失效的派生缓存，但那是优化，不是第二份真相。）

### 为什么 `note` 字段值得留

```python
● [t1] 把 core.py 的 print 换成 logging  // load() 已改；helper_* 仍有 print，属于后续清理
```

`note` 是**做完之后的关键结论**或**失败原因**。它是任务清单里唯一
"携带信息"而不只是"携带状态"的字段。

上下文被压缩之后，"我改了 load() 但没改 helper_*"这个细节
只会存在两个地方：摘要里（不保证）和 `note` 里（保证）。

### 为什么有 `failed` 而不只是三态

`pending / in_progress / completed` 三态没法表达"试过了，不行"。

没有 `failed`，模型只有两个选择：把做不成的任务永远挂在 `in_progress`
（然后卡死），或者悄悄标成 `completed`（然后骗自己）。

**状态机必须能表达真实发生的所有情况**，否则使用者会被迫说谎。

### 这和 s06 的 turn/step 有什么区别

三者是三个不同层次的"进度"：

```
Task    做什么      跨 turn，持久，模型定义
Turn    一次输入的排空    Harness 结构
Step    一次模型请求      Harness 结构
```

一个 task 可能横跨 5 个 turn，也可能在一个 turn 的 3 个 step 里就做完。
它们不是同一件事的不同粒度，而是**不同维度**。

---

## 与上一章相比发生了什么

| | s10 | s11 |
|---|---|---|
| 计划存在哪 | 模型说过的一段话 | **Harness 状态** |
| 压缩后计划 | 靠摘要"祈祷"保住 | **保证不受影响** |
| 新事件 | — | `task/write`（log-only，整表快照） |
| 新工具 | 8 个 | **9 个**（+`task_write`） |
| 新 section | — | `tasks`（order=15，很靠前） |
| 一致性 | — | 依赖校验 + 状态校验，失败不写入 |
| 进程重启 | 上下文可恢复 | **清单也可恢复**（从日志） |

---

## 真实系统里还有什么

- **更简的形态**：DeepSeek Harness 的 `todo/write` 只有 `content` 和三态 `status`，
  连 id 和依赖都没有 —— 因为整表覆盖写，条目不需要稳定身份。
  我们加了 `id` / `depends_on`，是为了能演示依赖校验这个教学点；
  真实产品里往往用不上，**能砍就砍**。
- **计划模式（plan mode）**：一种"先让模型把计划写出来给人看、
  人批准之后才开始执行"的协作模式。它是 task 系统之上的一层策略。
- **任务与子 Agent 的结合**：把一项 task 整个派给一个子 Agent（s09），
  完成后回填 `note`。s16 会做类似的事。
- **UI 渲染**：真实产品把清单渲染成可交互的 checklist，
  人可以直接勾选或增删 —— 这时 `task/write` 就有了第二个写入者。

---

## 自己动手改

1. **把 tasks section 删掉**
   `prompts.remove("tasks")`，重跑 demo（用真实模型）。
   观察模型在压缩之后还记不记得第 3 步。

2. **构造一个环**
   ```python
   task_write(tasks=[
     {"id":"a","title":"A","status":"pending","depends_on":["b"]},
     {"id":"b","title":"B","status":"pending","depends_on":["a"]},
   ])
   ```
   看 `任务依赖成环：a, b`。

3. **把校验改成"自动修复"**
   让 `validate` 发现前置未完成时，自动把 status 降回 `pending` 而不是报错。
   然后想想：模型还会知道自己错了吗？
   （**Harness 悄悄改模型的输入，模型就学不会。**）

4. **加一个字段再拿掉**
   给 `Task` 加 `priority`，跑一次，看 prompt 里 tasks 那块长了多少。
   再想想模型有没有因此做得更好。

5. **验证持久性**
   ```sh
   python s11_task_system/code.py     # 跑几轮，Ctrl-C
   # 然后写个小脚本：Session.load(...) → TaskStore(...).current()
   ```

---

## 下一章

现在 Agent 要跑测试：

```python
bash("python3 -m pytest")
```

这个项目的测试跑 **5 分钟**。

在这 5 分钟里，整个 Agent Loop **完全卡死**：

- 模型在等
- 用户在等
- 什么别的事都干不了

而模型本来完全可以在等测试的同时去读代码、准备下一步的修改。

更糟的是超时：s03 给 bash 设了 60 秒硬上限，所以这条命令根本**跑不完**，
模型会拿到一句"命令超时"，然后陷入"再试一次"的死循环。

工具调用是**同步**的 —— 调了就得等到结果。
但有些工作天然是**异步**的。

Harness 能不能提供一种"启动它，然后先去干别的，回头再来取结果"的机制？

→ [s12 — Background Jobs](../s12_background_jobs/)
