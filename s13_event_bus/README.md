# s13 — Event Bus

[s12](../s12_background_jobs/) → **s13** → [s14](../s14_plugin_system/) → … → s18

> 这一章**一个新功能都没加**。
>
> 它只是把东西搬了个家 —— 但搬完之后，前面 12 章攒下的横切逻辑
> 全都可以在不碰核心代码的前提下增删。

从这一章开始，我们进入课程的第三部分：**工业级 Harness 为什么需要
Event / Plugin / Capability / Isolation。**

---

## 上一章留下的问题

数一数 s12 的 `ToolExecutor.execute()` 承担了多少事：

```python
def execute(self, call_id, name, arguments, session, turn, step):
    session.append(EV_TOOL_CALL, ...)        # 1. 日志
    short = self.pre_execute(ctx, session)   # 2. 参数校验 3. 权限 4. 审批 5. 权限日志 6. trace
    result = ... self.run_body(ctx)          # 7. 执行
    result = self.post_execute(ctx, result)  # 8. 截断 9. trace
    session.append(EV_TOOL_RESULT, ...)      # 10. 日志
```

60 行，9 个职责。现在来三个需求：

1. "统计每个工具的平均耗时" → 改 `ToolExecutor`
2. "危险命令跑在沙箱里" → 改 `ToolExecutor`
3. "工具结果里的密钥要脱敏" → 改 `ToolExecutor`

**每加一个横切关注点，都要改同一个文件。**

而且它们互相不知道对方存在：脱敏在截断之前还是之后？沙箱和权限谁先跑？
这些顺序问题会以 `if` 的形式堆在 `execute()` 里，而且**顺序是隐式的** ——
取决于谁的代码写在前面。

更麻烦的是：这三个需求可能来自三个团队，或者根本就是可选功能 ——
CI 要沙箱，本地不要；企业版要脱敏，开源版不要。

s04 说过"权限不是 loop 里的一个 if，是管线上的一段"。
**但现在管线本身变成了新的万能修改点。**

---

## 这一章解决什么

```
        s12                              s13
┌────────────────────┐          ┌────────────────────────────┐
│ 写 tool/call 日志   │          │ bus.emit("tool/call")       │
│ 校验参数            │          │ bus.waterfall("pre")        │
│ 查权限              │          │ bus.waterfall("execute",    │
│ 问人                │          │      terminal=run_body)     │
│ 写 permission 日志  │   ───▶   │ bus.waterfall("post")       │
│ 执行                │          │ bus.emit("tool/result")     │
│ 截断                │          └────────────────────────────┘
│ trace               │
│ 写 tool/result 日志 │          权限 / 日志 / 截断 / 计时 / 脱敏
└────────────────────┘          都变成从外面挂进来的 listener
  60 行 / 9 个职责                 15 行 / 1 个职责
                                  **而且以后不会再改**
```

---

## 新增的核心概念

### 1. 两种派发方式，一个都不能少

```python
bus.on(event, fn, order)    # emit      —— 只能看见，改不了流程
bus.use(event, fn, order)   # waterfall —— 拿到 next，能包住、能短路
```

**emit（观察）**：审计、trace、指标上报。

```python
bus.on(EVT_TOOL_RESULT, lambda ctx: metrics.record(ctx.name))
```

**waterfall（中间件）**：权限、沙箱、超时、重试、计时。

```python
def timing(ctx, next_):
    t0 = time.perf_counter()
    next_()                       # 调用 = 委派给下一层
    record(time.perf_counter() - t0)
```

调 `next()` 就往下走，不调就**短路**（后面的全部不执行）。

**为什么必须区分这两种？**

因为"能不能改流程"是监听器的**契约**：

- 如果所有监听器都能短路，任何一个第三方插件都能悄悄让权限失效
- 如果都不能短路，权限就没法实现

> **把权力写进类型里，而不是靠约定。**

这个区分还有一个直接后果：

```python
def emit(self, event, *args):
    for _, _, fn in ...:
        try:
            fn(*args)
        except Exception as e:
            print(f"[bus] 观察者 {event} 抛异常：…")   # 吞掉，不影响主流程
```

观察者出错**不能**影响主流程 —— 它本来就没有改流程的权力。
但中间件不同：它有短路的权力，所以它抛异常就该往上冒。

demo 第 6 部分验证了这一点：挂一个 `lambda ctx: 1/0` 的观察者，工具照常返回。

### 2. waterfall 的实现只有 6 行

```python
def waterfall(self, event, ctx, terminal=lambda: None):
    chain = sorted(self._middleware.get(event, []), key=lambda e: e[0])

    def step(i):
        if i >= len(chain):
            terminal()
            return
        chain[i][2](ctx, lambda: step(i + 1))

    step(0)
```

用闭包把"下一层"包起来，交给当前层决定要不要调。这就是全部机械原理。

工具本体是 `tool/execute` 这个 waterfall 的 **terminal**：

```python
self.bus.waterfall(EVT_TOOL_EXECUTE, ctx, terminal=lambda: self.run_body(ctx))
```

### 3. order 让顺序从隐式变显式

```
waterfall tool/pre-execute   10:validate → 20:permission → 90:trace
waterfall tool/post-execute  10:redact   → 20:truncate
```

`redact`（10）在 `truncate`（20）**外层**，所以**先脱敏、后截断**。

反过来的话，被截断掉的那部分密钥就没被处理 ——
万一将来有人改截断策略，密钥就漏出去了。

在 s12 里这个顺序取决于代码位置，没人能讨论它。现在它是一个数字，
可以被写进文档、被评审、被覆盖。

### 4. 注册返回注销函数

```python
off = bus.on(EVT_TOOL_RESULT, audit)
...
off()      # 摘干净
```

这不是锦上添花：**s14 的插件卸载完全靠它。**

注册如果没有对应的撤销手段，插件系统就只能装不能卸。

### 5. EventBus 不只属于工具

`agent/pre-step` 是一个 waterfall，监听器可以改写或拒绝这一步的输入：

```python
def block_step(pre: StepPreCtx, next_):
    if any("危险" in it.content for it in pre.items):
        pre.rejected = True
        return                       # 短路
    next_()
```

```
Turn 2: steps=0 reason=rejected
模型一次都没被调用。
```

**s06 里那个只能靠"没有输入"制造的 0 step turn，现在有了真正的机制。**

注意 `StepPreCtx` 的字段：

```python
@dataclass
class StepPreCtx:
    turn: int
    step: int
    items: list[InboxItem]
    rejected: bool = False
    reject_reason: str = ""
```

它拿不到 `session`（改不了历史），拿不到 `registry`（改不了工具集）。
**权力边界写在 dataclass 的字段里。**

---

## 最小架构图

```
   ToolExecutor.execute()
        │
        ├─ emit      tool/call         ──▶ [session-log]
        │
        ├─ waterfall tool/pre-execute  ──▶ [validate] → [permission] → [trace]
        │                                      │            │
        │                                   短路 = 拒绝，工具本体不执行
        │
        ├─ waterfall tool/execute      ──▶ [timing] ──▶ ((run_body))  ← terminal
        │
        ├─ waterfall tool/post-execute ──▶ [redact] → [truncate]
        │
        └─ emit      tool/result       ──▶ [session-log] → [trace]

   run_turn()
        └─ waterfall agent/pre-step    ──▶ [guard]  ← 可拒绝整个 step
```

---

## 跑一下

```sh
python s13_event_bus/code.py --demo
python s13_event_bus/code.py --demo --debug
```

demo 有 7 个部分，每一个都在证明"核心代码不用改"：

```
【3】脱敏 listener
  磁盘上的真实内容：   API_KEY=sk-abcdef0123456789
  模型看到的：         API_KEY=***

【4】计时 listener（s12 做不到的需求之一）
  bash   1 次   平均  12.92 ms
  read   1 次   平均   0.08 ms

【5】运行时加一个 listener，再摘掉
  挂上之后：['glob:ok']
  摘掉之后：['glob:ok']   （没有新增）

【6】观察者抛异常，工具照常返回
【7】pre-step 拦掉一整个 step  →  steps=0 reason=rejected
```

---

## 为什么这样设计

### 为什么子 Agent 共享同一条总线

```python
child_executor = ToolExecutor(child_registry, BUS)     # 同一条 BUS
```

权限、脱敏、审计对子 Agent 一样生效。

如果给子 Agent 单独建一条总线，那些策略就会**静默失效** ——
这类"看起来能跑、其实少了一层防护"的 bug 极难发现。

（真实系统会做得更细：子 Agent 有自己的 scope，可以注册**额外的**监听器，
但继承全局的那些。我们这里用了最简单的做法。）

### `install_default_listeners` 是什么

```python
def install_default_listeners(bus, policy, approver, tracer, timing_stats):
    bus.on(EVT_TOOL_CALL, listener_log_tool_call, order=10, owner="session-log")
    bus.use(EVT_TOOL_PRE, make_validate_listener(), order=10, owner="validate")
    ...
```

它其实就是一个**插件** —— 只是还没有被正式命名、还不能卸载。

s14 会给它一个名字、一个生命周期、一个卸载路径。
**这就是"抽象由痛点触发"：先看到它长什么样，再给它一个类型。**

### 这算不算过度抽象

值得警惕，所以检查一下：

| 抽象 | 有几个实现 | 值不值 |
|---|---|---|
| `EventBus` | 1 个 | 值 —— 它的价值不在多态，在于**解耦注册方和调用方** |
| `emit` vs `waterfall` | 2 种，都在用 | 值 |
| listener | 8 个，且能自由增删 | 值 |

反例是这样的：只有一个实现却定义五层接口。我们没有 `IEventBus`、
没有 `EventBusFactory`、没有 `AbstractListener` —— 那些才是过度抽象。

判据不是"有没有引入抽象"，是"**这个抽象让什么变简单了**"。
这里的答案很具体：ToolExecutor 从 60 行变成 15 行，而且从此不再改。

---

## 与上一章相比发生了什么

| | s12 | s13 |
|---|---|---|
| `ToolExecutor` | 60 行 / 9 职责 | **15 行 / 1 职责** |
| 权限 | Executor 的方法 | `tool/pre-execute` 监听器 |
| 日志 | Executor 里 append | `tool/call` `tool/result` 监听器 |
| 截断 | Executor 的方法 | `tool/post-execute` 监听器 |
| trace | Executor 持有 Tracer | 监听器 |
| 计时 | **做不到** | `tool/execute` 监听器（8 行） |
| 脱敏 | **做不到** | `tool/post-execute` 监听器（10 行） |
| 顺序 | 隐式（代码位置） | **显式（order 数字）** |
| 增删 | 改代码 | 注册/注销函数 |
| 拒绝一整个 step | 只能靠"没有输入" | `agent/pre-step` 短路 |
| 新增功能 | — | **零** |

---

## 真实系统里还有什么

- **四种派发模式**：Cordis 有 `emit` / `waterfall` / `parallel` / `serial`，
  分别对应"观察 / 包裹 / 并行 / 有序且有返回值"。我们只取了两种。
- **单调守卫**：在 pre waterfall 之后还有一层**只能拒绝、不能放行**的守卫。
  这样第三方插件无法把别人拒掉的东西放回来 —— 权限的可组合性需要这种单调性。
  我们的 waterfall 里，一个 order 很小的监听器理论上能"吃掉"权限拒绝。
- **事件的类型契约**：真实系统用 TypeScript 声明合并给每个事件一个精确类型，
  并生成一份"谁生产、谁消费"的目录。我们用字符串常量 + dataclass。
- **scope 过滤**：一个事件可以只派发给某个 agent 的监听器。
- **异常语义**：中间件抛异常时，真实系统会把它规范化成 `isError` 的结果，
  而不是让整个 turn 崩掉。我们的中间件异常会往上冒。

---

## 自己动手改

1. **写一个超时中间件**
   ```python
   def timeout(ctx, next_):
       # 提示：真正的超时要靠 signal / 线程，这里可以先记录"疑似超时"
       ...
   bus.use(EVT_TOOL_EXECUTE, timeout, order=5)     # 比 timing 更外层
   ```

2. **把权限关掉**
   在 `install_default_listeners` 里删掉 permission 那一行。
   注意：**你没有注释任何 ToolExecutor 的代码。**

3. **调换 redact 和 truncate 的 order**
   把 redact 改成 30，构造一个超长的、密钥在末尾的输出，看密钥漏出来。

4. **加一个"工具失败率"观察者**
   ```python
   bus.on(EVT_TOOL_RESULT, lambda ctx: fails.setdefault(ctx.name, []).append(
       bool(ctx.result and ctx.result.is_error)))
   ```

5. **验证 emit 不能改流程**
   写一个 `bus.on(EVT_TOOL_RESULT, lambda ctx: setattr(ctx, "result", None))`，
   看它能不能影响返回值。（能改 ctx，但 executor 已经取走结果了 ——
   想想这算不算一个应该修的漏洞。）

6. **用 pre-step 实现"只读模式"**
   拒绝一切包含写操作意图的输入？不 —— 那是在替模型判断。
   正确做法是在 `tool/pre-execute` 上挂一个把写工具全 DENY 的中间件。
   体会这两个挂载点的区别。

---

## 下一章

现在打开 `install_default_listeners`：

```python
def install_default_listeners(bus, policy, approver, tracer, timing_stats):
    bus.on(EVT_TOOL_CALL, listener_log_tool_call, ...)
    bus.use(EVT_TOOL_PRE, make_validate_listener(), ...)
    bus.use(EVT_TOOL_PRE, make_permission_listener(policy, approver), ...)
    ...
```

再看一眼 `demo()` 和 `main()` 的开头：

```python
SKILLS = SkillRegistry(...)
TASKS = TaskStore(session)
JOBS = JobRegistry()
PROVIDER_FOR_SUBAGENT = ...
SUMMARIZER = ...
```

**装配逻辑正在失控。**

- 一堆模块级全局变量（`SKILLS` / `TASKS` / `JOBS` / `INBOX` / `RT` / `BUS`…）
- `demo()` 和 `main()` 里各抄了一遍同样的装配代码
- 想关掉"后台任务"这个功能？要删工具注册、删 prompt section、删全局变量、删装配代码 —— **四个地方**
- 想给别人一个"只读版 Agent"？没有任何单元可以整体拿掉

问题的本质是：**这些功能没有边界**。

"后台任务"这个功能由 1 个 Registry + 4 个工具 + 1 个 prompt section +
2 个事件类型 + 1 个全局变量组成，但代码里没有任何东西把它们**框起来**。

能不能让一个功能成为一个**可以整体装上、整体卸下**的单元？

→ [s14 — Plugin System](../s14_plugin_system/)
