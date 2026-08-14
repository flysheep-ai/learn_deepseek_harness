# s03 — Tool Registry

**English version: [README.en.md](README.en.md)**

[s02](../s02_tool_use/) → **s03** → [s04](../s04_permission/) → … → s18

> **Tool 是 Harness 提供给模型的 Action Space。**
>
> 这一章把这句话变成一个具体的对象。

---

## 上一章留下的问题

s02 只有一个工具，所以这样写没问题：

```python
if call.name == "bash":
    output = run_bash(call.arguments["command"], cwd)
```

现在加到 6 个工具，问题立刻出现三个：

### 问题 1：schema 和实现会漂移

```python
TOOLS = [{"name": "edit", "parameters": {..."old_text"...}}]   # 在文件顶部
...
elif call.name == "edit":
    run_edit(path, old, new)                                   # 在 200 行之外
```

有一天你把参数名从 `old_text` 改成 `old`，改了实现忘了改 schema。
模型按旧 schema 发参数，`TypeError`，会话直接死。
**两处定义同一件事，就一定会漂移。**

### 问题 2：模型的参数不可信

模型会漏必填参数，会多传参数，会幻觉出不存在的工具名。这些**全是常态**：

```python
run_edit(**{"path": "a.py"})              # TypeError: 缺 old_text
run_edit(**{"path": "a.py", "foo": 1})    # TypeError: 意外参数 foo
handler = TOOL_HANDLERS["teleport"]        # KeyError
```

每一个都会让 `agent_loop` 崩掉。

### 问题 3：共享逻辑要抄 N 遍

想给所有工具加"执行前检查"？在 6 个 `elif` 分支里各写一遍。
明天加 sandbox，再写 6 遍。

---

## 这一章解决什么

引入四个对象，一个也不多：

```
Tool          一个能力：schema + 实现，绑在一起
ToolResult    执行结果：content（给模型看）+ is_error（给 Harness 看）
ToolRegistry  行动空间的唯一来源：装工具、按名取、吐 schema
ToolExecutor  把一次 tool_call 变成一个 ToolResult；共享逻辑只写一遍
```

---

## 新增的核心概念

### 1. Tool：schema 和实现绑死

```python
@registry.tool(
    "edit", "把文件中某段精确文本替换成新文本（只替换第一处）。",
    {"type": "object",
     "properties": {"path": {...}, "old_text": {...}, "new_text": {...}},
     "required": ["path", "old_text", "new_text"]},
)
def run_edit(path: str, old_text: str, new_text: str) -> str:
    ...
```

schema 和函数**在源码里挨着**。改参数名时两边都在你眼前。
问题 1 从"要靠自觉"变成"结构上不可能"。

### 2. ToolRegistry：模型行动空间的唯一来源

```python
provider.chat(messages, tools=registry.schemas(), system=system)
```

从此"模型看得见的工具"和"实际能执行的工具"是**同一个集合**。

这个"唯一来源"的性质，到 s09 会变得非常重要：
子 Agent 只能用 `read` / `grep`，我们靠 `registry.restrict()` 实现。
那时"被过滤掉的工具既不出现在 prompt 里、也拒绝执行"这个一致性，
正是靠 registry 是唯一来源才守得住。

### 3. ToolResult：两个受众，两个字段

```python
@dataclass(frozen=True)
class ToolResult:
    content: str          # 给模型看的
    is_error: bool = False # 给 Harness 看的
```

为什么不直接返回 `str`？因为 Harness 自己需要知道成败：
s04 的权限拒绝、s13 的失败率统计、s18 的重试判断都要读 `is_error`，
而模型只需要 `content`。

### 4. ToolExecutor：共享逻辑的挂载点

```python
def execute(self, name, arguments) -> ToolResult:
    tool = self.registry.get(name)
    if tool is None:      return ToolResult(f"错误：没有名为 '{name}' 的工具。可用工具：…", is_error=True)
    missing = [...]
    if missing:           return ToolResult(f"错误：{name} 缺少必填参数：…", is_error=True)
    cleaned = {k: v for k, v in arguments.items() if k in known}   # 丢掉多余参数
    try:
        return ToolResult(tool.handler(**cleaned))
    except Exception as e:
        return ToolResult(f"错误：{type(e).__name__}: {e}", is_error=True)
```

三种模型侧错误全部变成**模型能读的字符串**，一个异常都不往外冒。

注意错误信息的写法：

```
错误：没有名为 'teleport' 的工具。可用工具：bash, read, write, edit, glob, grep
错误：edit 缺少必填参数：old_text, new_text
错误：在 hello.py 中找不到该文本。先用 read 确认当前内容。
```

每一条都告诉模型**下一步该怎么办**。只说"失败了"，模型就得多浪费一步去猜。

> **给模型的失败信息，要可行动。**

---

## 最小架构图

```
                 ┌─────────────┐
   注册期  Tool ─▶│ToolRegistry │
                 └──────┬──────┘
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
      schemas()               get(name)
            │                       │
            ▼                       ▼
      ┌──────────┐          ┌──────────────┐
      │   LLM    │─tool_call▶│ ToolExecutor │─▶ ToolResult
      └──────────┘          └──────────────┘
                                    │
                             未知工具 / 缺参数 /
                             多余参数 / 异常兜底
```

---

## 跑一下

```sh
python s03_tool_registry/code.py --demo
```

demo 里模型走了一遍真实工作流（glob → read → grep → edit → bash 验证），
然后**故意**触发三条错误路径：

```
→ teleport  to='mars'
  ✗ 错误：没有名为 'teleport' 的工具。可用工具：bash, read, write, edit, glob, grep
→ edit      path='hello.py'
  ✗ 错误：edit 缺少必填参数：old_text, new_text
→ read      path='../../../etc/passwd'
  ✗ 错误：ValueError: 路径越界，超出工作区
```

三次都没有异常冒出来，会话继续。

---

## 为什么这样设计

### 为什么 agent_loop 一行都没重写

对比 s02 和 s03 的循环，只差两处：

```python
# s02
reply = provider.chat(messages, tools=TOOLS, system=system)
if call.name == "bash":
    output = run_bash(call.arguments["command"], cwd)

# s03
reply = provider.chat(messages, tools=executor.registry.schemas(), system=system)
result = executor.execute(call.name, call.arguments)
```

这件事本身就是一个结论：

> **好的 Harness 抽象不会改变 loop 的形状。**
> 如果加一个机制需要重写 `agent_loop`，八成是挂载点选错了。

s13 会把这句话变成一条硬约束（"不要不断修改 Agent Loop"）。
从 s03 到 s18，`agent_loop` 的骨架基本没再动过 —— 变的都是它周围的东西。

### 为什么 `safe_path` 现在就要有

```python
def safe_path(p: str) -> Path:
    root = WORKSPACE.resolve()
    path = (root / p).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"路径越界，超出工作区：{p}")
    return path
```

模型可以请求任何路径，**能不能碰是 Harness 说了算**。

这是第一次出现"Harness 限制模型"的代码。s04 会把它正式化成权限模型；
s15 会把它变成可替换的文件系统 provider（换成 `MemoryFileSystem`，
整个工具集就跑在内存里了）。

### 为什么 ToolExecutor 现在这么"薄"

它现在只做三件事，确实单薄。但它是**位置**：

```
s03  execute() = 校验 + 调用
s04  execute() = pre → 校验 → execute → post          ← 权限住进 pre
s13  execute() = 发事件，让插件挂上去                    ← pre/post 变成事件
```

**先有位置，才谈得上往上挂东西。** 这是"抽象由痛点触发"的另一面：
痛点已经出现（共享逻辑抄 6 遍），所以现在建这个位置是合理的；
但不能因为 s13 要用事件，就在 s03 提前把 EventBus 搬进来。

### 为什么 registry 的 schema() 里没有 handler

```python
def schema(self):
    return {"name": ..., "description": ..., "parameters": ...}   # 没有 handler
```

注册表持有的信息**远多于**模型能看见的。

真实 Harness 里这条边界是用显式白名单守住的 ——
超时预算、并发安全标记、UI 渲染函数都在 `ToolDefinition` 里，
但一个都不能泄漏进模型请求。我们这里只是简单地不放进去，
但你要意识到这是一条**被刻意维护**的边界，而不是碰巧。

---

## 与上一章相比发生了什么

| | s02 | s03 |
|---|---|---|
| 工具数 | 1 | 6（bash / read / write / edit / glob / grep） |
| schema 与实现 | 分离在两处 | **绑在 `Tool` 上** |
| 分派 | `if/elif` | `registry.get(name)` |
| 模型发错参数 | `TypeError`，会话死 | 变成一条可读的错误结果 |
| 幻觉工具名 | 一句"未知工具" | 列出可用工具，模型能自己纠正 |
| 共享逻辑 | 每个分支抄一遍 | `ToolExecutor` 一处 |
| 结果类型 | `str` | `ToolResult(content, is_error)` |
| `agent_loop` | — | **只改了 2 行** |

---

## 真实系统里还有什么

- **完整的 JSON Schema 校验**：我们只查了"必填参数在不在"和"多余参数丢掉"，
  没查类型。真实系统会用 schema 校验器把 `limit: "10"`（字符串）拒掉或强转。
- **工具输出的结构化契约**：DeepSeek Harness 要求每个工具声明 `output.schema`，
  执行结果先校验成规范 JSON，再由 `render()` 投影成模型看的文本。
  这样同一个结果既能喂模型，也能渲染成 UI 卡片。我们直接返回字符串。
- **per-tool 超时与并发标记**：`timeoutMs` / `isConcurrencySafe` 这类字段
  属于 registry，永远不进模型请求。
- **工具的 UI 呈现**：`presentCall` / `presentResult` 让一次调用在界面上
  显示成"正在读取 main.py"而不是一坨 JSON。

---

## 自己动手改

1. **加一个工具，数一下改了几处**
   加 `list_dir`（列目录）。s02 要改 2 处（schema + elif），s03 只要写 1 个带装饰器的函数。

2. **让两个工具重名**
   注册两个都叫 `read` 的工具。`ToolRegistry.register` 会立刻抛
   `工具重名：read`。想想为什么这个错误应该在**启动时**炸，而不是运行时。

3. **删掉参数校验**
   把 `missing` 那段注释掉，跑 demo。观察 `edit(path='hello.py')`
   怎么从"一条友好错误"变成 `TypeError` 让整个 loop 崩掉。

4. **给模型一个"半吊子"工具**
   写一个 handler 直接 `raise RuntimeError("boom")`，看它怎么被
   `ToolExecutor` 兜住变成 `错误：RuntimeError: boom`，会话继续。

5. **观察行动空间**
   `print(registry.schemas())` —— 这一坨 JSON 就是模型对世界的**全部**认知。
   它能做的一切都在里面，一个字都不多。

---

## 下一章

现在模型有 6 个工具，其中包括：

```python
bash("rm -rf /")
write("~/.ssh/authorized_keys", "...")
```

`safe_path` 拦住了文件工具的路径越界，但 **`bash` 什么都能干**。

Registry 解决了"怎么给模型能力"，但完全没解决"要不要给"。

> Harness 不只是给 Agent 能力，也负责**限制** Agent 能力。

问题来了：限制该写在哪？写进 `run_bash` 里？那 6 个工具要各写一遍，
回到 s03 之前的老问题了。写进 `ToolExecutor.execute()` 里？那它凭什么
知道 `bash` 危险而 `read` 安全？

→ [s04 — Permission](../s04_permission/)
