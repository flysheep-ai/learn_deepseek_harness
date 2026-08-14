# s15 — Capability Seams

**English version: [README.en.md](README.en.md)**

[s14](../s14_plugin_system/) → **s15** → [s16](../s16_agent_team/) → … → s18

> 为什么 Filesystem / Shell / LLM 应该通过接口解耦？
>
> 因为**换一个 provider，整个产品跟着换 —— 工具一个字不改。**

---

## 上一章留下的问题

打开 s14 的 `CoreToolsPlugin`，看 `read` / `write` / `bash` 的实现 ——
它们直接调 `pathlib` 和 `subprocess`。

现在产品提需求："让 Agent 跑在远程沙箱里。"

- 文件操作要变成走沙箱的 RPC
- shell 执行要变成沙箱里的进程
- 权限、脱敏、审计……全部不用改（它们是事件层，与实现无关）

但看代码：

```python
def _read(path, limit):
    lines = _safe_path(cwd, path).read_text()     # ← 实现写死在 handler 里
```

要换实现，就得改**每一个工具**的 handler ——
我们又回到了 s13 的老路上，只是这次不是"改 ToolExecutor"，是"改 6 个工具的 handler"。

---

## 这一章解决什么

把"文件系统"和"shell"这两个**能力**做成 seam：

```
一个 seam = 三个角色

Service Definition（接口）      Provider（实现）          Consumer（使用方）
┌──────────────────┐         ┌──────────────────┐      ┌──────────────────┐
│ class FileSystem │  ◀─实─  │ LocalFileSystem   │      │ read / write /   │
│   read/write/…   │   现──  │ MemoryFileSystem  │      │ edit / glob /    │
│ class Shell      │         │ LocalShell        │      │ grep / bash      │
│   run(...)       │         │ DryRunShell       │  ─用─│ 只依赖接口        │
└──────────────────┘         └──────────────────┘      └──────────────────┘
  Consumer 不 import Provider；Provider 按 key 注册，运行时才决定用哪个
```

demo 直接展示三个世界：

```
世界 A  LocalFileSystem + LocalShell    真实读写、真实执行
世界 B  MemoryFileSystem + DryRunShell  纯内存、命令只预演
世界 C  LocalFileSystem + DryRunShell   读本地、执行只预演（预览模式）
```

**三个世界跑的是完全相同的工具代码。**

---

## 新增的核心概念

### 1. 三角色

**Service Definition（接口）**——不 import 任何实现：

```python
class FileSystem(Protocol):
    def read(self, path: str, limit_lines: int | None = None) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    ...
```

**Provider（实现）**——`LocalFileSystem` / `MemoryFileSystem` / `DryRunShell`。

**Consumer（使用方）**——`CoreToolsPlugin` 的 6 个工具：

```python
def setup(self, ctx) -> None:
    fs = ctx.require("fs")       # 只拿到接口，不知道是谁实现的
    shell = ctx.require("shell")

    @ctx.tool("read", ...)
    def _read(path, limit=None):
        return fs.read(path, limit_lines=limit)     # ← 只依赖接口
```

> Consumer 不 import Provider。背后是本地磁盘还是内存还是远程沙箱，
> 与它无关。

换 provider 的入口只有一个参数：

```python
h = build_harness("minimal", cwd, ..., fs=mem, shell=DryRunShell())
```

### 2. 接口刻意保持很小

```python
class FileSystem(Protocol):
    def read(...); def write(...); def edit(...)
    def glob(...); def grep(...); def exists(...)
```

只有工具真正需要的六个方法。

**接口每多一个方法，所有 provider 都要实现它一遍。**
如果你加了 `def chmod`，`MemoryFileSystem` 和未来的远程 provider 都得跟。
接口的大小是"能力"和"实现成本"之间的权衡。

### 3. 越界检查住在 provider 里

```python
# LocalFileSystem
def read(self, path, limit_lines=None):
    lines = self._resolve(path).read_text()      # _resolve 里做越界检查
```

s03 的时候，`safe_path` 是写在工具 handler 旁边的；
s14 的时候，它在每个工具的闭包里各调一次。

现在它在 provider 里**做一次**。好处不只是少写代码：

> 换成远程沙箱 provider 时，越界语义由沙箱自己保证 ——
> 工具完全不用改，因为它从头到尾就不知道路径长什么样。

### 4. LLM 也是一个 seam

回头看一眼 `harness_llm.py` —— 它早就是了：

```
Definition: LLMProvider Protocol
Providers:  OpenAICompatProvider / AnthropicProvider / ScriptedProvider
Consumer:   run_turn（只调 provider.chat）
```

这个项目从 s01 就在用一个 seam，只是 s15 才给它这个名字。

而且这个 seam 的价值已经兑现过很多次：`--demo` 换 `ScriptedProvider`、
测试换假模型、用户换 DeepSeek/OpenAI —— 每一次都是"换 provider"。

---

## 最小架构图

```
             build_harness(profile, fs=?, shell=?)
                       │
                       ▼
   CapabilityPlugin  ── 把 fs / shell 装进 services
                       │
        ┌──────────────┼──────────────────┐
        ▼              ▼                  ▼
   CoreToolsPlugin  JobPlugin        （未来：sandbox、LSP…）
     require("fs")   require("shell")
        │              │
        ▼              ▼
   工具的 handler 里只有接口调用，没有实现细节
```

DeepSeek Harness 文档里那句话值得整句抄下来：

> Filesystem and subprocess providers share one execution world,
> so pointing them at a remote sandbox moves Bash, PTY, and LSP with them,
> **with no provider forks**.

文件系统和进程执行共享同一个"执行世界"。把这个世界指向远程沙箱，
bash、终端、LSP 一起搬过去 —— 不需要为每个能力各写一套沙箱适配。

---

## 跑一下

```sh
python s15_capability_seams/code.py --demo
python s15_capability_seams/code.py --demo --debug
```

注意世界 B 的输出：

```
read(app.py)  →     1  VERSION = "0.9.0"      ← 内存里的内容
write(new.txt) → 已写入 new.txt（5 字节）
grep(TODO)    → README.md:3:TODO: 加配置
bash(rm -rf /) → [dry-run] 本应执行：rm -rf /   ← 没有执行任何东西
```

**没有任何磁盘 I/O，没有任何进程启动**，但六个工具全部正常工作。
这就是 provider 替换的完整效果。

---

## 为什么这样设计

### 为什么 seam 和 plugin 是两个概念

- **plugin** 回答"系统有哪些功能"（可装配、可卸载）
- **seam** 回答"一个能力的实现是谁"（可替换、可多提供者）

一个 plugin（`CapabilityPlugin`）负责把 seam 的 provider 装进去；
多个 plugin 可以共享同一个 seam（`CoreToolsPlugin` 和 `JobPlugin` 都用 shell）。

s14 的 `provide/require` 是 seam 的雏形，这一章只是把"按 key 取服务"
这个想法推到了它自然的结论：**定义、实现、消费三方彻底分离。**

### 为什么 MemoryFileSystem 值得写 70 行

两个理由：

1. **测试**。不确定性的工具测试跑在内存里，秒级、无副作用。
   这是真实 Harness 的日常 —— DeepSeek Harness 里每个 provider
   都有对应的内存/回放实现用于测试。
2. **教学**。它用最便宜的方式证明了"换 provider = 换世界"。

### 为什么不把 JobPlugin 也换成 seam

它还是直接 `subprocess.Popen`。这是**刻意的对照**：

真实 Harness 里 job 的执行也走 shell seam（这样后台任务同样能被沙箱化），
但我们留一个"没走 seam"的插件在场上，你就能看到两种写法的差别 ——
前者换世界一个参数，后者换世界要改实现。

### 这算不算过度抽象？

检查一下：

| 抽象 | 有几个实现 | 值不值 |
|---|---|---|
| `FileSystem` | 2 个 | 值 —— 第二个实现（Memory）当天就用了 |
| `Shell` | 2 个 | 值 —— DryRunShell 组出"预览模式" |
| `FileEntry` / `ShellResult` | — | 值 —— 纯数据、可 JSON 化，远程 provider 能传 |

判据不变：**这些抽象让什么变简单了？**
答案：换执行环境从"改 6 个 handler"变成"传一个参数"。

---

## 与上一章相比发生了什么

| | s14 | s15 |
|---|---|---|
| 工具的底层 | 直接调 pathlib / subprocess | **只依赖 fs / shell 接口** |
| 换执行环境 | 改每个 handler | **传 `fs=` / `shell=` 参数** |
| 越界检查 | 每个工具里调 `_safe_path` | **provider 里做一次** |
| 测试文件工具 | 需要真实磁盘 | 跑在 MemoryFileSystem 上 |
| 预览模式 | 不存在 | DryRunShell 组出来 |
| 新对象 | — | `FileSystem` `Shell`（Definition）+ 4 个 Provider + `CapabilityPlugin` |

---

## 真实系统里还有什么

- **同一个 seam 的多个 provider 共存**：DeepSeek Harness 的 subagent
  就是按名注册多个 provider 的（in-process / fork / ACP / Codex…），
  模型可以选择派给谁。而 bash 只有一个执行者。我们只做了"一个 seam 一个 provider"。
- **Provider 的额外能力探测**：沙箱 provider 可能有配额、网络限制，
  Consumer 需要探测这些能力。这是接口设计的另一个维度，我们没碰。
- **Definition 不只是 Protocol**：真实系统的 Definition 还带事件
  （`fs/write-intent` 这类），让 policy 能挂在能力上而不挂在使用方上。
- **组合执行世界**：dsh 里 `filesystem` 和 `subprocess` 两个 provider
  共享一个 execution world，所以切一次沙箱四个能力一起搬。
  我们的 `fs` / `shell` 是两个独立参数，组合能力弱于"世界"抽象，
  但已足够讲清概念。

---

## 自己动手改

1. **写一个 CountingFileSystem**
   包装 LocalFileSystem，统计读了多少次、多少字节。
   然后把它传进 build_harness —— 工具一个不改，度量就有了。
   （这就是"装饰器 provider"模式，权限、缓存、限流都是这么做的。）

2. **写一个 RemoteStubFileSystem**
   所有方法 raise `NotImplementedError("走 RPC")`。
   装上它跑一轮 —— 你会看到 Harness 其余部分完全照常运转，
   只有 fs 相关的工具报错。**这证明了解耦是真的。**

3. **把 JobPlugin 改成走 shell seam**
   让它 `shell = ctx.require("shell")`，`start_bash` 里调 `shell.run`。
   （提示：job 要的是异步，seam 的 `run` 是同步 —— 想想这个缝该怎么改，
   这就是真实系统里"同步 seam 之上做异步 job"要解决的问题。）

4. **给 MemoryFileSystem 加目录列表**
   让 `glob("*")` 也能返回目录名。看 `dirs` 集合怎么用。

5. **换世界重跑同一个会话**
   用同一个 session 事件流，一次配 Local、一次配 Memory，
   观察工具结果哪里不同 —— 这说明"确定性"是 provider 的性质，
   不是 Harness 的性质。

---

## 下一章

s09 有了 subagent，s14 有了插件，s15 有了 seam。现在把它们组合起来：

> "帮我查一下这个 bug 的原因，写个修复，再让另一个人检查一下。"

这个任务需要**多个 Agent 协作**：

- 一个去查原因（explorer）
- 一个去写修复（editor）
- 一个去审查（reviewer）

自然的做法是写死流程：

```python
if task_type == "research":    research_agent()
elif task_type == "fix":       coding_agent()
elif task_type == "review":    review_agent()
```

**这是全课程最需要警惕的一刻** —— 上面的代码是 Harness 在替模型决定
"创建谁、委派什么、什么时候收集结果"。

能不能做到：Harness 只提供 spawn / send / receive / status，
而**协作策略完全由模型自己决定**？

→ [s16 — Agent Team](../s16_agent_team/)
