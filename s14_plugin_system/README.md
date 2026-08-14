# s14 — Plugin System

[s13](../s13_event_bus/) → **s14** → [s15](../s15_capability_seams/) → … → s18

> **"Everything is a plugin" 到底解决了什么？**
>
> 不是炫技。是让一个功能有**边界**。

---

## 上一章留下的问题

s13 把横切逻辑变成了 listener，装配代码集中在
`install_default_listeners()` 里。但停下来看一眼 `demo()` 和 `main()` 的开头：

```python
SKILLS = SkillRegistry(...)              # 全局变量 ×8
TASKS  = TaskStore(session)
JOBS   = JobRegistry()
PROVIDER_FOR_SUBAGENT = ...
SUMMARIZER = ...
```

**装配逻辑正在失控：**

| 问题 | 具体表现 |
|---|---|
| 无边界 | "后台任务" = 1 个 Registry + 4 个工具 + 1 个 prompt 段 + 2 个事件类型 + 1 个全局变量 + 1 个 pre-step 监听器 —— 但代码里没有任何东西把它们**框起来** |
| 卸载要改 N 处 | 关掉"后台任务"要删：工具注册、prompt section、全局变量、装配代码 —— **四个地方**，而且容易漏掉 pre-step 里那个 `pump_jobs()` |
| 装配抄两遍 | `demo()` 和 `main()` 里各有一份，改一处忘另一处 |
| 没有产品形态 | 想给一个"只读版 Agent"？没有任何单元可以整体拿掉 |

问题的本质：**这些功能没有边界。**

---

## 这一章解决什么

把散落的装配收编成插件：

```
s13                               s14
┌────────────────────────┐        ┌──────────────────────────────┐
│ SKILLS = SkillRegistry()│        │ harness.use(CoreToolsPlugin())│
│ TASKS  = TaskStore(...) │        │ harness.use(PermissionPlugin())│
│ JOBS   = JobRegistry()  │        │ harness.use(SkillPlugin())    │
│ registry.tool(...) ×13  │        │ harness.use(TaskPlugin())     │
│ prompts.section(...) ×8 │        │ harness.use(JobPlugin())      │
│ bus.on/use(...)     ×9  │        │ …                             │
│ 全局变量            ×8  │        └──────────────────────────────┘
│ demo() 和 main() 各抄一遍│         每个 plugin 自带工具 + prompt
└────────────────────────┘         + 监听器 + service
                                   可整体卸载、可自由组合
```

**关键机制只有一条：注册是可逆的 effect。**

---

## 新增的核心概念

### 1. Plugin：一个名字 + 一个 setup

```python
class Plugin(Protocol):
    name: str
    def setup(self, ctx: "PluginContext") -> None: ...
```

**刻意没有 `teardown` 方法。**

因为手写清理一定会漏：setup 里加了一行注册，teardown 里忘了加对应的一行 ——
这种 bug 静默、难查、且必然发生。

我们的做法：**注册这个动作本身返回撤销函数**，PluginContext 自动收集：

```python
class ToolRegistry:
    def register(self, tool: Tool) -> Callable[[], None]:
        ...
        return lambda: self._tools.pop(tool.name, None)   # ← 可逆
```

```python
class PluginContext:
    def tool(self, name, description, parameters):
        def deco(fn):
            off = self.harness.tools.register(Tool(name, description, parameters, fn))
            self._disposers.append(off)     # ← 自动收集
            return fn
        return deco

    def unload(self):
        for off in reversed(self._disposers):   # 逆序
            off()
```

这是 s13 就埋下的伏笔（`bus.on/use` 返回注销函数），现在是收获的时候。

### 2. PluginContext：插件能碰的世界只有这五个方法

```python
ctx.tool(...)     # 注册工具
ctx.section(...)  # 注册 prompt 段
ctx.on(...)       # 注册观察者
ctx.use(...)      # 注册中间件
ctx.provide(key, service)   # 提供一个 service
ctx.require(key)  # 取用别人提供的 service
```

插件**不能**直接碰 harness 的内部结构。

注意 `require` 的失败方式：

```python
raise RuntimeError(
    f"插件 {self.plugin_name} 需要 service '{key}'，但没有插件提供它。"
    f"当前已有：{', '.join(self.harness.services)}")
```

**早失败，且说清楚缺谁。** 不做"自动排序依赖"这种魔法 ——
显式的加载顺序比隐式的推导好读。

### 3. service 按 key 取用，不按类型、不靠 import

```python
ctx.provide("timing", stats)       # TimingPlugin 提供
ctx.require("timing")              # 别的插件取用
```

这样"谁实现了 tasks"这件事在**运行时**才决定。
s15 会把"按 key 取服务"这个想法推到底，变成正式的 capability seam。

### 4. Harness：没有特权的核心

```python
class Harness:
    def __init__(self, session, cwd):
        self.bus = EventBus()
        self.tools = ToolRegistry()
        self.prompts = SystemPromptRegistry()
        ...
```

它自己**什么功能都没有** —— bash 是插件给的，权限是插件给的，
连"把工具调用写进日志"都是 `SessionLogPlugin` 给的。

> **There is no privileged core to patch.**
>
> 你想改任何行为，都不需要修改这个类。

这句话的实际证据在 `SessionLogPlugin` 的注释里：

> 卸载它之后 Agent 照常工作，但日志里就没有 tool/call 和 tool/result 了。
> 这件事本身很能说明问题：**日志不是副作用，日志是主干。**

### 5. Profile：一份插件清单 = 一个产品形态

```python
h = build_harness("full", ...)       # 14 个插件 13 个工具
h = build_harness("minimal", ...)    #  8 个插件  6 个工具
h = build_harness("readonly", ...)   # 10 个插件  8 个工具
```

加一个产品形态 = 加一个分支，**不需要改任何插件、run_turn、ToolExecutor**。

注意 readonly 里"不装"和"装了但禁用"的区别：

> 不装意味着模型的 prompt 里根本没有那些工具，它不会想着去试。

---

## 最小架构图

```
                 build_harness(profile, ...)
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
   CoreToolsPlugin  PermissionPlugin    JobPlugin
        │               │                   │
        │  setup(ctx)   │                   │
        └───────┬───────┴───────────────────┘
                ▼
        ┌──────────────┐
        │PluginContext │  tool() / section() / on() / use() / provide()
        │  _disposers  │  ← 每个注册的撤销函数都收集在这里
        └──────┬───────┘
               │ unload() = 逆序执行全部
               ▼
        ┌──────────────┐
        │   Harness    │  bus / tools / prompts / services / inbox / rt
        │  （没有特权） │  ← 卸载任何插件都不用改它
        └──────────────┘
```

---

## 跑一下

```sh
python s14_plugin_system/code.py --demo
python s14_plugin_system/code.py --demo --debug
python s14_plugin_system/code.py --profile readonly    # 真实模型下的只读版
```

demo 的六幕：

```
【1】14 个插件贡献了 13 个工具、8 个 prompt 段、5 个 service、11 个监听器
【2】各插件协同工作（任务清单、脱敏、权限全在）
【3】unload("jobs")：工具 -4、prompt -1、service -1、监听器 -1，一行代码
【4】卸载之后模型确实拿不到 bash_background 了
【5】三个 profile 三种产品形态；readonly 的 write 被权限拒绝
【6】写一个 30 行的 NotePlugin，不碰任何已有代码
```

---

## 为什么这样设计

### 为什么不用装饰器注册工具了

s03 的写法：

```python
@registry.tool("bash", "…", {…})
def run_bash(command): ...
```

s14 的写法：

```python
@ctx.tool("bash", "…", {…})
def _bash(command): ...
```

变化在于：工具现在是**闭包**，捕获 plugin 自己的 cwd。
s13 里 `run_bash` 要读全局 `WORKSPACE`，所以它不可能同时服务两个工作区；
现在一个进程里可以有两套完整的 harness，各自有自己的 cwd。

### 为什么 SkillPlugin 把 service、工具、prompt 段放在同一个插件里

因为它们是**同一个功能**的三样东西。

卸载它，三样一起消失。这就是"功能有边界"的字面意思。

s13 里"关掉技能功能"意味着：删全局变量 + 删工具注册 + 删 prompt section +
删装配代码 —— 四个地方，靠人记。现在是 `h.unload("skills")`。

### 为什么 RuntimeContext 不再有 skills/tasks/jobs 字段

s07–s12 每加一个功能就往 RuntimeContext 加一个字段，
等于每个功能都在修改一个共享的数据结构 —— 那和"每加一个功能就改 ToolExecutor"是同一个病。

现在功能自带状态（放在自己的 service 里），prompt section 直接从 service 读。
RuntimeContext 只留**所有功能都需要**的东西。

### 这算不算过度抽象？

检查一下（s17 会做一次完整的全库审查，这里先看本章）：

| 抽象 | 有几个实现 | 值不值 |
|---|---|---|
| `Plugin` / `PluginContext` | 13 个插件 | 值 —— 且 demo 第 6 幕证明加新插件零侵入 |
| `Harness` | 1 个 | 值 —— 它把"系统有哪些功能"推迟到装配时决定 |
| service 按 key 取 | 5 个 service | 值 —— s15 会继续推 |

判据还是那句：**这个抽象让什么变简单了？**
答案是具体的：卸载功能从"改 4 处"变成"一行"，
产品形态从"没有"变成"一个 profile 参数"。

---

## 与上一章相比发生了什么

| | s13 | s14 |
|---|---|---|
| 装配 | `install_default_listeners()` + 一堆全局变量 | **plugin 自带一切** |
| 卸载一个功能 | 改 4 个地方，靠人记 | `h.unload(name)` |
| 新功能 | 挂 listener（只覆盖横切逻辑） | **写一个 plugin**（工具+prompt+监听器+service 都有） |
| 产品形态 | 无 | profile（full / minimal / readonly） |
| 全局变量 | 8 个 | **0 个** |
| 工具定义 | 读全局 `WORKSPACE` | **闭包捕获自己的 cwd** |
| `run_turn` | 引用 SKILLS/TASKS/JOBS/SUMMARIZER | **不引用任何功能** |
| 压缩/注入 | run_turn 里的硬编码调用 | 挂在 pre-step 上的插件监听器 |

`run_turn` 现在完全不引用任何功能 —— 卸载任何插件它都不用改。

---

## 真实系统里还有什么

- **依赖声明**：Cordis 用 `inject` 声明服务依赖，装载器据此**自动排序**。
  我们靠"按 require 的顺序手动 use" + 缺失时早失败。自动排序更强，
  但隐式推导会带来新的读代码成本 —— 教学项目选显式。
- **可逆 effect 库**：Cordis 里 `ctx.effect()` 是核心 API，所有注册
  （包括监听器）都返回 disposer，卸载是运行时保证而非约定。
  我们的 `_disposers` 是它的最小版本。
- **配置驱动的装配**：真实产品的 profile 不是代码里的 elif，
  而是一份 YAML 配置 + 可 patch 的层叠。那是"部署"问题，不是"理解 Harness"问题。
- **per-agent 插件作用域**：插件可以只挂载到某一个 agent（s09 的 scope 概念）。
- **热重载**：卸载 + 重新 setup 就能热更一个插件，前提是插件状态都在
  service 里且注销干净 —— 我们的结构已经支持，只是没演示。

---

## 自己动手改

1. **卸载 session-log 再看日志**
   `h.unload("session-log")`，跑一轮，然后打开 session.jsonl ——
   你会发现工具调用和结果都没有了（所以模型也"失明"了）。
   这证明日志不是副作用，是主干。

2. **写一个 WelcomerPlugin**
   挂在 `EVT_TURN_START` 上打印问候。然后试着自己卸载它。

3. **给 readonly 加一个说明**
   在 readonly 的 IdentityPlugin 文本里加一句"你处于只读模式"。
   注意：**你只改了 build_harness 的一行**。

4. **交换两个插件的加载顺序**
   把 `TaskPlugin()` 移到 `CoreToolsPlugin()` 之前 —— 会崩吗？
   （不会：它们不互相依赖。构造一个"任务依赖"的例子体会 `require` 的意义。）

5. **把 NotePlugin 挂到 readonly 上**
   在 `build_harness` 的 readonly 分支加一行 `h.use(NotePlugin())`。
   体验一下"组合"。

---

## 下一章

打开 `CoreToolsPlugin`，看 `read` / `write` / `bash` 的实现 ——
它们直接调 `pathlib` 和 `subprocess`。

现在产品提需求："让 Agent 跑在远程沙箱里。"

问题来了：

- 文件操作要变成走沙箱的 RPC
- shell 执行要变成沙箱里的进程
- **权限、脱敏、审计……全部不用改**（它们是事件层，与实现无关）

但看代码：`read` 里写着 `_safe_path(cwd, path).read_text()`。
要换实现，就得改**每一个工具**的 handler —— 我们又回到了 s13 的老路上，
只是这次不是"改 ToolExecutor"，是"改 6 个工具的 handler"。

有没有办法让"文件系统"和"shell"这两个**能力**本身可以被替换，
而工具只是它们的**消费者**，根本不知道背后是谁？

比如：一套 harness 跑在 `LocalFileSystem` 上，另一套跑在
`MemoryFileSystem` 上 —— 其余 1000 行代码**完全相同**？

→ [s15 — Capability Seams](../s15_capability_seams/)
