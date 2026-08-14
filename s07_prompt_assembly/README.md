# s07 — Prompt Assembly

**English version: [README.en.md](README.en.md)**

[s06](../s06_turn_and_step/) → **s07** → [s08](../s08_skill_loading/) → … → s18

> system prompt 是一个常量，还是一个**运行时产物**？

---

## 上一章留下的问题

s06 的 system prompt 长这样：

```python
def make_system(cwd, reg):
    return (f"你是一个编程 Agent，工作目录是 {cwd}。\n"
            f"可用工具：{', '.join(reg.names())}。\n直接动手，不要解释。")
```

139 个字符，还行。但接下来的章节要往里塞：

| 章 | 要塞的内容 | 什么时候需要 |
|---|---|---|
| s08 | 可用技能清单 | 总是 |
| s09 | 子 Agent 能力说明 | 只有主 Agent 需要 |
| s11 | 当前任务列表 | 有任务时 |
| s12 | 后台任务状态 | 有正在跑的 job 时 |
| — | 项目约定（AGENTS.md） | 有这个文件时 |

继续往这个 f-string 里拼，会得到两个后果：

**1. 它变成所有功能的争抢点。** 加一个技能要改它，加一个工具要改它，
加一个任务系统还要改它。一个 200 行的字符串，谁也不敢动。

**2. 它无法表达"这次不需要"。** 没有 AGENTS.md 也得写一行"项目规范：无"。
没有任务也得写"当前任务：无"。**每次请求都在为不存在的东西付 token。**

---

## 这一章解决什么

把巨型字符串拆成**可注册、可排序、可条件跳过**的块：

```
PromptSection(identity)      ┐
PromptSection(environment)   │
PromptSection(project)       ├─▶ assemble(ctx) ─▶ system prompt
PromptSection(tools)         │        ▲
PromptSection(session_state) ┘        │
                                RuntimeContext
```

并且加一条工业不变量：**把组装结果写进日志**（`request/header` 事件），
这样"每一次模型请求"都能从日志完整重建。

---

## 新增的核心概念

### 1. PromptSection：三个字段就够

```python
@dataclass(frozen=True)
class PromptSection:
    name: str                                        # 身份，同名即替换
    order: int                                       # 排序权重
    render: Callable[[RuntimeContext], str | None]   # None = 这次不出现
```

**`render` 返回 `None` 是整章的关键。**

```python
@prompts.section("project", 30)
def _project(ctx) -> str | None:
    if not ctx.project_notes:
        return None          # 没有 AGENTS.md？这一块整个消失。
    return f"# 项目约定\n{ctx.project_notes.strip()}"
```

> prompt 不是模板填空，是**按当前状态挑选内容**。

demo 的第一部分直接对比给你看：

```
冷启动（无 AGENTS.md）        共 222 字符
  identity       34
  environment    94
  project         -   （本次不出现）
  tools          94
  session_state   -   （本次不出现）

有 AGENTS.md 的同一个工作区    共 292 字符
  project        70   ← 出现了
```

**同样的 section 集合，不同的运行时状态，不同的 prompt。**

### 2. RuntimeContext：section 唯一能读的东西

```python
@dataclass
class RuntimeContext:
    cwd: Path
    tool_names: list[str]
    turn: int = 0
    step: int = 0
    project_notes: str | None = None
    files_read: list[str] = field(default_factory=list)
```

section **只能读它**，不能读全局变量。这条约束换来的是可测试性：
给一个假的 `RuntimeContext`，就能断言某个 section 渲染出什么，不用启动 agent。

它会随章节增长（s08 加 skills、s11 加 tasks、s12 加 jobs），
但增长的是**数据**，不是 `assemble()` 的逻辑。

### 3. SystemPromptRegistry：二十几行

```python
def assemble(self, ctx) -> str:
    parts = []
    for sec in sorted(self._sections.values(), key=lambda s: s.order):
        text = sec.render(ctx)
        if text:
            parts.append(text.strip())
    return "\n\n".join(parts)
```

它的价值不在逻辑复杂，而在于**把"谁能往 prompt 里加东西"变成一个注册动作**。

demo 第 4 部分演示了运行时加一块，一行已有代码都不用改：

```python
@prompts.section("safety_note", 60)
def _safety(c): return "# 注意\n本环境禁止访问网络。"
```

到 s14 有了插件系统，插件就是这样贡献 prompt 的 —— 谁也不用碰别人的代码。

### 4. request/header：请求本身也要可重建

```python
if system != last_header:
    session.append(EV_REQUEST_HEADER, {
        "turn": turn, "step": step,
        "system": system,
        "tools": [t["name"] for t in tools],
        "sections": [n for n, size in prompts.explain(rt) if size],
    })
```

s05 说"凡是能进模型请求的，都必须能从日志重建"。但 s05/s06 只记了 messages ——
**prompt 和工具清单没记**。于是日志能还原"模型说了什么"，
却还原不了"模型当时被告知了什么规则"。回放是残缺的。

现在补上了。而且只在**变化时**记：

```
日志里的 request/header 快照：2 条（这一轮跑了 4 个 step）
  # 5 step 1  sections=identity,environment,project,tools           298 字符
  #13 step 2  sections=…,session_state                              338 字符
```

step 2/3/4 的 prompt 完全相同，只记一条。全量记太吵，不记则日志残缺，
**变化时记**是两者的交点。

---

## 最小架构图

```
            RuntimeContext
       ┌────────┴─────────────────────┐
       │ cwd  tools  turn  step        │
       │ project_notes  files_read     │
       └────────┬─────────────────────┘
                │
    ┌───────────▼────────────┐
    │ SystemPromptRegistry   │   按 order 排序
    │   10 identity          │   逐块 render(ctx)
    │   20 environment       │   None → 跳过
    │   30 project      ←条件│
    │   40 tools             │
    │   50 session_state ←条件│
    └───────────┬────────────┘
                │ assemble()
                ▼
         system prompt ──┬──▶ provider.chat(system=…)
                         │
                         └──▶ session: request/header（变化时）
```

---

## 跑一下

```sh
python s07_prompt_assembly/code.py --demo
python s07_prompt_assembly/code.py --demo --show-prompt   # 打印完整 prompt
python s07_prompt_assembly/code.py --demo --debug
```

`--debug` 里盯住 `system=` 那个数字：

```
[step 1]  → model request   messages=1 tools=6 system=298chars
[step 2]  → model request   messages=3 tools=6 system=338chars   ← 变长了
[step 3]  → model request   messages=5 tools=6 system=338chars
```

step 1 之后模型读了 `app.py`，`session_state` 这一块被激活，prompt 自己变长了 40 字符。

**工具执行改变了 ctx，ctx 改变了 prompt，prompt 改变了模型看到的内容。**
这条链就是"prompt 是运行时产物"最直观的证据。

---

## 为什么这样设计

### 为什么每一步都重新组装，不是每轮一次

因为 `session_state` 这类 section 的内容会在**同一轮内**变化 ——
刚读完一个文件，下一步就该知道。

组装是纯函数（结果只取决于 `ctx`），所以它便宜、可测、可预测。
每步重算的代价远小于"缓存 + 失效"带来的 bug。

这和 s05 的 `derive_messages` 每步重算是同一个理由。

### 为什么 session_state 里不写步号

```python
return f"# 当前进度（第 {ctx.turn} 轮）\n本轮已读取：…"   # 没有 step
```

写了步号，prompt 就**每一步都不同**。后果有两个：

1. `request/header` 快照失去去重，日志里塞满几乎一样的 prompt
2. provider 端的 prompt 缓存全部失效（真实场景里这是实打实的钱）

只放模型真正需要的信息。**"能加"不等于"该加"** ——
prompt 里的每一个字都在花钱，而且都在稀释模型的注意力。

### 为什么工具的**参数**不写进 prompt

```python
return f"# 工具\n可用工具：{', '.join(ctx.tool_names)}\n读文件优先用 read 而不是 bash cat；…"
```

只写"怎么用"，不写"有哪些参数"。参数在 tool schema 里，由 provider 单独发送。

在 prompt 里重复一遍是很常见的浪费，而且两处会漂移 ——
和 s03 讲的 schema/实现漂移是同一个病。

### 为什么 section 有 name 而不只是一个函数列表

`name` 让 section 可以被**替换和移除**：

```python
prompts.register(PromptSection("identity", 10, my_custom_identity))   # 覆盖
prompts.remove("safety_note")
```

s14 的插件卸载时要把自己注册的 section 撤掉，s09 的子 Agent 要用
完全不同的 `identity`。没有 name，这两件事都做不了。

---

## 与上一章相比发生了什么

| | s06 | s07 |
|---|---|---|
| system prompt | 一个 f-string 函数 | **注册表 + 有序 section** |
| "这次不需要某块" | 做不到 | `render` 返回 `None` |
| 谁能加内容 | 改 `make_system()` | `@prompts.section(...)` 注册 |
| 组装时机 | 每轮一次 | **每步一次** |
| prompt 是否进日志 | 否 | `request/header`（变化时快照） |
| 新概念 | — | `PromptSection` / `RuntimeContext` / `SystemPromptRegistry` |
| `run_turn` 参数 | `system: str` | `rt: RuntimeContext` |

---

## 真实系统里还有什么

- **scope（作用域）**：真实 Harness 的 section 可以是**全局**的，
  也可以只属于某一个 agent。同名时"最近的 scope 赢" ——
  这就是"per-agent 人格"的实现方式。s09 会用一个简化版。
- **waterfall 拦截**：组装本身是一个可拦截的事件，
  监听器能在最终 prompt 上再做一次改写。s13 会具备这个能力。
- **prompt 缓存**：真实系统会把稳定前缀标记出来供 provider 缓存。
  这就是"别在 prompt 里放每步都变的东西"在生产上的直接价值。
- **token 预算分配**：section 之间要抢有限的 token。
  真实系统会给每块设上限、按优先级截断。我们全量拼接。

---

## 自己动手改

1. **加一个 section**
   ```python
   @prompts.section("git_state", 25)
   def _git(ctx):
       import subprocess
       b = subprocess.run(["git", "branch", "--show-current"], cwd=ctx.cwd,
                          capture_output=True, text=True).stdout.strip()
       return f"# Git\n当前分支：{b}" if b else None
   ```
   注意 `order=25` 把它插在 environment 和 project 之间，**你没有改任何已有代码**。

2. **让一块消失**
   删掉 demo 工作区里的 `AGENTS.md`（改 `build_demo_workspace(with_notes=False)`），
   看 `project` 那一行变成"（本次不出现）"，总字符数掉 70。

3. **故意让 prompt 每步都变**
   把步号加回 `session_state`，重跑，数一下 `request/header` 快照变成几条。
   这就是"prompt 里放易变内容"的真实代价。

4. **测一个 section**
   ```python
   ctx = RuntimeContext(cwd=Path("/tmp"), tool_names=["read"], project_notes="X")
   assert "X" in _project(ctx)
   assert _project(RuntimeContext(cwd=Path("/tmp"), tool_names=[])) is None
   ```
   不用启动 agent，不用调模型。这就是"section 只能读 ctx"换来的东西。

5. **看看 prompt 到底长什么样**
   ```sh
   python s07_prompt_assembly/code.py --demo --show-prompt
   ```

---

## 下一章

现在假设你想让 Agent 懂一些专门知识：

- 团队的 git 工作流（什么时候开分支、commit message 怎么写）
- 项目的 Python 规范（用 ruff、类型标注要求、测试放哪）
- 数据库迁移的正确姿势

最直接的做法是加几个 section：

```python
@prompts.section("git_guide", 60)
def _git_guide(ctx): return open("guides/git.md").read()      # 3000 字
```

然后你发现：**每一次请求都在为它付费。**
用户问"这个函数是干嘛的"，跟 git 工作流一点关系都没有，
但那 3000 字照样发出去了。10 个这样的知识块 = 每次请求 30000 字。

而且这 30000 字里，模型真正需要的可能只有 500 字。

能不能先只告诉模型**有哪些知识可用**，等它真的需要时再加载全文？

→ [s08 — Skill Loading](../s08_skill_loading/)
