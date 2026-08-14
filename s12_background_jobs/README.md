# s12 — Background Jobs

[s11](../s11_task_system/) → **s12** → [s13](../s13_event_bus/) → … → s18

> 工具调用天然是**同步**的：调了就得等到结果。
>
> 但有些工作天然是**异步**的。

---

## 上一章留下的问题

Agent 要跑测试：

```python
bash("python3 -m pytest")
```

这个项目的测试跑 5 分钟。这 5 分钟里，整个 Agent Loop **完全卡死** ——
模型在等、用户在等、什么别的事都干不了。

而模型本来完全可以在等测试的同时去读代码、改别的地方。

更糟的是超时：s03 给 bash 设了 60 秒硬上限，所以这条命令**根本跑不完**，
模型会拿到一句"命令超时"，然后陷入"再试一次"的死循环。

---

## 这一章解决什么

给 Harness 加第二种执行形状：

```
同步 tool call                    异步 job
┌──────────────────┐             ┌──────────────────────────────┐
│ bash("pytest")   │             │ bash_background("pytest")     │
│   ⏳ 阻塞 5 分钟  │             │   → "job bash-1 已启动"（即刻）│
│   ← 结果          │             │ 模型继续读代码、改别的地方…     │
└──────────────────┘             │ [job bash-1 完成] ← Harness 注入│
                                 └──────────────────────────────┘
```

demo 的实测：

```
【对照】bash("python3 slow_test.py") 阻塞了 1.2 秒，期间整个 loop 什么都干不了
【Turn 1】用了 0.0s（没有等测试跑完），6 个 step
```

---

## 新增的核心概念

### 1. Job ≠ Task（这两个词很容易混）

```
Task  模型的**意图**   "把 print 换成 logging"   跨 turn，持久，模型写
Job   一次**执行**     "跑 pytest 这条命令"      有进程，Harness 管
```

一个 Task 可能触发多个 Job；一个 Job 也可能和任何 Task 都无关。
**它们是两个维度，不是两个粒度。**

### 2. Registry 管生命周期，Producer 管怎么跑

```python
class JobRegistry:
    def start_bash(self, command, cwd, session) -> Job: ...
    def get(self, job_id) -> Job | None: ...
    def running(self) -> list[Job]: ...
    def stop(self, job_id) -> str: ...
    def take_finished_unnotified(self) -> list[Job]: ...
```

Registry 管**身份和生命周期**（id、状态、取消、快照），
Producer 管**怎么跑**（这里是 `subprocess` + 线程）。

这样加一种新的 job kind（比如"后台跑一个子 Agent"）不需要动 Registry。
s16 会用到这一点。

### 3. `pump_jobs()` —— 整章真正的重点

后台执行本身不难（起个线程就行）。难的是**结果怎么回到模型那里**。

工具调用有天然的返回路径（`tool_result`）。异步任务**没有** ——
它完成的时候，模型可能正在干别的，甚至已经停下来了。

所以 Harness 必须**主动推**。推到哪？还是 inbox：

| 章 | 来源 | source |
|---|---|---|
| s06 | 用户中途插话 | `steering` |
| s08 | 技能正文 | `skill` |
| s12 | job 完成通知 | `job` |

demo 里能直接看到：

```
#  4 user/message  source=user   跑一下 slow_test.py，同时把 cli.py 里的 print 换成 logging。
# 53 user/message  source=job    [后台任务 bash-1 已失败]
```

**三种来源完全不同，但只有一条入口、一套认领机制。**

这就是 s06 建立 inbox 抽象的回报 —— 现在加一种"Harness 想让模型知道的事"，
不需要发明任何新通道。

### 4. `notified` 标记

```python
def take_finished_unnotified(self) -> list[Job]:
    for j in self._jobs.values():
        if j.status != "running" and not j.notified:
            j.notified = True
            out.append(j)
```

没有这个标记，每一步都会重复注入同一条完成通知，把上下文刷爆。

这是"事件驱动 + 轮询"混合结构里最常见的一个坑。

### 5. `job_output` 不阻塞

```python
if job.status == "running":
    return f"job {job_id} 还在运行（{job.elapsed:.1f}s）。先去做别的，完成时会通知你。"
```

如果这里 `join()` 等一下，异步就**退化回同步**了。

诚实地告诉模型"还没好"，让它自己决定是等还是先干别的 ——
这又回到了那条铁律：**观察必须诚实，决策交给模型。**

---

## 最小架构图

```
   模型：bash_background("pytest")
        │
        ▼
   JobRegistry.start_bash()
        │  ├─▶ session: job/start（log-only）
        │  └─▶ threading.Thread(subprocess.Popen)  ⟳ 后台跑着
        │
        └─▶ 立即返回 "job bash-1 已启动"    ← tool_result，模型继续下一步
                    ⋮
              （模型干了别的 4 个 step，turn 自然结束）
                    ⋮
   job 结束
        │
        ▼
   pump_jobs()   ← 每个 step 认领输入之前调用
        │  ├─▶ session: job/end（log-only）
        │  └─▶ Inbox.put(结果, source="job")
        │
        ▼
   下一个 step 认领 → user/message（SURFACE）→ 进入模型上下文
```

---

## 跑一下

```sh
python s12_background_jobs/code.py --demo
python s12_background_jobs/code.py --demo --debug
```

demo 的两个 turn 很关键：

- **Turn 1**：模型起了后台任务，然后改了 `cli.py`，查了一次 job 状态，结束。**全程 0.0s。**
- **Turn 2**：**输入不是用户打的字**，是 Harness 注入的 job 完成通知。
  模型读输出、更新任务清单为 `failed` 并写下失败原因。

---

## 为什么这样设计

### 为什么 turn 结束时**不等**后台任务

```python
else:
    # 注意：有 job 在跑不算"欠着"。
    # 这一轮诚实地结束，之后由 job 完成通知唤醒新的一轮。
    reason = "natural-stop"
    break
```

如果在这里死等 job，异步就变回同步了 —— 只是把等待从工具里挪到了 turn 结尾。

s06 定义 turn 是"一次输入的排空"。后台任务不是输入，它是**将来会产生输入的东西**。
所以正确的做法是：结束这一轮，让通知去唤醒下一轮。

### 为什么 `pump_jobs` 放在 `claim()` **之前**

```python
if prompt_registry is None:
    pump_jobs(session, inbox)
claimed = inbox.claim()
```

这样刚完成的任务能被**当前这一步**看到，而不是等到下一步。
差一步就是差一次模型调用的延迟。

### 为什么 `job/start` 和 `job/end` 是 log-only

模型看到的是两样东西：`bash_background` 的返回值，和后来注入的那条完成通知。
再让它看一遍 Harness 的内部记账就是重复。

但这两条事件对**人**有价值：回放日志时能精确知道任务什么时候起的、
跑了多久、退出码是多少。

### `daemon=True` 的代价

```python
threading.Thread(target=runner, daemon=True).start()
```

主程序退出时不被后台任务拖住 —— 代价是**进程退出时未完成的 job 直接消失**。

真实系统会在退出前显式收尾，或者把 job 交给一个独立的守护进程。
我们这里选了简单，但你应该知道这个选择的后果。

---

## 与上一章相比发生了什么

| | s11 | s12 |
|---|---|---|
| 长命令 | 阻塞整个 loop（且 60s 超时） | **后台跑，立即返回** |
| 新对象 | — | `Job` / `JobRegistry` |
| 新工具 | 9 个 | **13 个**（+`bash_background` `job_status` `job_output` `job_stop`） |
| 新事件 | — | `job/start` `job/end`（log-only） |
| 新 section | — | `jobs`（只列 running 的） |
| 异步结果的路径 | — | `pump_jobs()` → inbox → `user/message` |
| turn 的驱动 | 只有用户输入 | **也可以是 job 完成通知** |

---

## 真实系统里还有什么

- **多种 job kind**：DeepSeek Harness 的 `JobKindMap` 是可扩展的
  （`bash` / `subagent` / …），Registry 把 kind 当成不透明的 id 命名空间。
- **归属与权限**：谁能停掉一个 job？真实系统靠 owner 授权，
  而不是"知道 id 就能操作"。我们没做。
- **流式输出**：跑着的任务应该能被"看一眼当前进度"（`tail`），
  而不是只能等它结束。我们只在结束时给全量输出。
- **持久化**：进程重启后 job 就没了。真实系统把 job 状态也写进存储。
- **定时任务**：cron 风格的调度是同一套 job 机制加一个触发器。
- **背压**：同时起 50 个 job 会打爆机器。真实系统有并发上限和队列。

---

## 自己动手改

1. **把 `job_output` 改成阻塞的**
   加一句 `while job.status == "running": time.sleep(0.1)`。
   跑 demo，看 Turn 1 的耗时从 0.0s 变成 1.2s ——
   **异步机制被一行代码毁掉了。**

2. **去掉 `notified` 标记**
   看同一条完成通知怎么被重复注入。

3. **加一个 job kind**
   写 `start_subagent(preset, task)`：在后台跑一个 s09 的子 Agent。
   （Registry 一行不用改 —— 这就是"Registry 管生命周期、Producer 管怎么跑"的价值。）

4. **测试 `job_stop`**
   起一个 `sleep 60`，然后 `job_stop`。检查 `job/end` 的 status 是 `killed`。

5. **观察 turn 的两种驱动**
   ```sh
   python s12_background_jobs/code.py     # 真实模型
   > 后台跑 sleep 3 && echo done
   > （直接回车）      # 空输入：如果 job 没完成，会得到一个 0 step 的 turn
   > （再回车）        # job 完成了：通知被认领，模型开始处理
   ```

---

## 下一章

停下来数一数，`ToolExecutor` 现在承担了多少事：

```python
def execute(self, call_id, name, arguments, session, turn, step):
    session.append(EV_TOOL_CALL, ...)        # 1. 日志
    short = self.pre_execute(ctx, session)   # 2. 参数校验 3. 权限 4. 审批 5. 权限日志
    result = ... self.run_body(ctx)          # 6. 执行
    result = self.post_execute(ctx, result)  # 7. 截断 8. trace
    session.append(EV_TOOL_RESULT, ...)      # 9. 日志
```

现在假设产品经理来了三个需求：

1. "帮我统计每个工具的平均耗时" → 改 `ToolExecutor`
2. "危险命令要跑在沙箱里" → 改 `ToolExecutor`
3. "工具结果里的密钥要脱敏" → 改 `ToolExecutor`

**每加一个横切关注点，都要改同一个文件。**

而且它们互相不知道对方存在：脱敏要在截断之前还是之后？沙箱和权限谁先跑？
这些顺序问题会以 `if` 的形式堆在 `execute()` 里。

更麻烦的是：这三个需求可能来自三个不同的团队，或者根本就是可选功能 ——
CI 环境要沙箱，本地开发不要；企业版要脱敏，开源版不要。

s04 说过"权限不是 loop 里的一个 if，是管线上的一段"。
但现在管线本身变成了新的万能修改点。

能不能让这些关注点**从外面挂进来**，而不是写进 `ToolExecutor`？

→ [s13 — Event Bus](../s13_event_bus/)
