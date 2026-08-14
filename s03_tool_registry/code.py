#!/usr/bin/env python3
"""s03 — Tool Registry

    Tool ──▶ ToolRegistry ──▶ schemas()  ──▶ 模型看到的行动空间
                  │
                  └────────▶ ToolExecutor ──▶ 执行 ──▶ ToolResult

这一章回答：**工具多起来之后，Harness 需要什么结构？**

s02 的 `if call.name == "bash"` 在第 5 个工具上会散架。这一章把它换成
注册表 + 执行器，并且第一次给出一个正式说法：

    Tool 是 Harness 提供给模型的 Action Space。

运行：
    python s03_tool_registry/code.py --demo
    python s03_tool_registry/code.py
"""

import glob as globlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

MAX_STEPS = 20


# ══════════════════════════════════════════════════════════════════
# s03 新增：Tool / ToolResult / ToolRegistry / ToolExecutor
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的结果。

    为什么不直接用 str？因为 Harness 需要**区分**"工具说了什么"和
    "工具是否成功"。s04 的权限拒绝、s13 的失败率统计、s18 的重试
    都要读 is_error。而模型侧只需要 content —— 两个受众，两个字段。
    """

    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    """一个可被模型调用的能力。

    关键设计：**schema 和实现绑在同一个对象上。**

    s02 里 schema 在 TOOLS 列表、实现在 elif 分支，两处会对不上 ——
    改了参数名忘了改另一边，模型就会按旧 schema 发参数，然后 TypeError。
    绑在一起之后，这类错误在结构上就不可能发生了。
    """

    name: str
    description: str
    parameters: dict[str, Any]           # JSON Schema
    handler: Callable[..., str]

    @property
    def required(self) -> list[str]:
        return list(self.parameters.get("required", []))

    def schema(self) -> dict[str, Any]:
        """给模型看的部分。

        注意 handler **不在**里面。注册表持有的信息远多于模型能看见的，
        这个"内部字段绝不泄漏到模型请求里"的边界，在真实 Harness 里
        是靠显式白名单守住的（超时预算、并发标记、UI 渲染函数都不能外泄）。
        """
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    """工具注册表 —— 模型行动空间的**唯一**来源。

    它只有三个职责，刻意保持得很小：
      · 装工具
      · 按名字取工具
      · 吐出给模型的 schema 列表

    "按名字取"这件事替代了 s02 的 elif 链。更重要的是 schemas() ——
    从此"模型看得见的工具"和"实际能执行的工具"是**同一个集合**，
    不可能再出现"prompt 里写了但执行时没有"的漂移。

    s09 会给它加一个 restrict()：子 Agent 只能看到工具的一个子集。
    到那时你会发现，"被过滤掉的工具既不在 prompt 里、也拒绝执行"
    这个一致性，正是靠 registry 是唯一来源才能保证的。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, parameters: dict[str, Any]) -> Callable:
        """装饰器写法，让 schema 和实现在源码里也挨着。"""

        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            self.register(Tool(name=name, description=description, parameters=parameters, handler=fn))
            return fn

        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]


class ToolExecutor:
    """把一次 tool_call 变成一个 ToolResult。

    它存在的理由是：**所有工具共享的逻辑，只该写一遍。**

    s02 里如果你想给每个工具加"参数校验"，得在 5 个 elif 分支里各写一遍。
    现在只有这一个地方。

    这个类现在只做三件事（未知工具 / 缺参数 / 异常兜底），看起来有点单薄。
    但它是后面所有东西的挂载点：
      s04 在 execute() 里插进 pre → execute → post 三段管线
      s13 把这三段变成事件，让插件挂上去
    先有位置，才谈得上往上挂东西。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.registry.get(name)

        # ① 模型幻觉出了不存在的工具。
        #    把可用工具列出来 —— 这是给模型的**可执行反馈**，
        #    它读完就能自己改用正确的名字。只说"未知工具"是浪费一步。
        if tool is None:
            return ToolResult(
                f"错误：没有名为 '{name}' 的工具。可用工具：{', '.join(self.registry.names())}",
                is_error=True,
            )

        # ② 模型漏了必填参数。
        #    这在真实使用里非常常见，尤其是参数多的工具。
        #    不校验的话就是一个 TypeError 冒到 loop 外面，会话直接结束。
        missing = [k for k in tool.required if k not in arguments]
        if missing:
            return ToolResult(f"错误：{name} 缺少必填参数：{', '.join(missing)}", is_error=True)

        # ③ 模型多传了参数（也很常见）。
        #    直接 **arguments 会 TypeError，所以按 schema 过滤一遍。
        known = set(tool.parameters.get("properties", {}))
        cleaned = {k: v for k, v in arguments.items() if k in known}

        try:
            return ToolResult(tool.handler(**cleaned))
        except Exception as e:  # noqa: BLE001
            # 工具体内的任何意外都止步于此。
            # 理由和 s02 一样：工具失败是业务，不是崩溃 ——
            # 但现在这条保证对**所有**工具生效，而不是靠每个工具自觉。
            return ToolResult(f"错误：{type(e).__name__}: {e}", is_error=True)


# ══════════════════════════════════════════════════════════════════
# s03 新增：六个基础工具
# ══════════════════════════════════════════════════════════════════

registry = ToolRegistry()

# 所有文件工具都被关在这个目录里。main() 会按运行模式改写它。
WORKSPACE = Path.cwd()


def safe_path(p: str) -> Path:
    """把相对路径解析到工作区内，并拒绝逃逸。

    `../../etc/passwd`、绝对路径、符号链接都会被 resolve() 摊平，
    然后这里做一次归属检查。

    请注意它的性质：这是 Harness 在**限制**模型的能力。
    模型可以请求任何路径，能不能碰是 Harness 说了算。
    s04 会把这个想法正式化成权限模型，s15 会把它做成可替换的文件系统 provider。
    """
    root = WORKSPACE.resolve()
    path = (root / p).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"路径越界，超出工作区：{p}")
    return path


@registry.tool(
    "bash", "在工作目录下执行一条 shell 命令，返回 stdout+stderr。",
    {"type": "object",
     "properties": {"command": {"type": "string", "description": "要执行的命令"}},
     "required": ["command"]},
)
def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKSPACE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            out = f"[exit {r.returncode}]\n{out}"
        return out[:20000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：命令超时（60 秒）"


@registry.tool(
    "read", "读取文件内容，返回带行号的文本。",
    {"type": "object",
     "properties": {"path": {"type": "string"},
                    "limit": {"type": "integer", "description": "最多返回多少行"}},
     "required": ["path"]},
)
def run_read(path: str, limit: int | None = None) -> str:
    lines = safe_path(path).read_text(encoding="utf-8").splitlines()
    shown = lines[:limit] if limit else lines
    # 带行号返回：模型后续要靠行号定位、要靠它判断文件有没有被截断。
    body = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(shown, 1))
    if limit and len(lines) > limit:
        body += f"\n… 还有 {len(lines) - limit} 行未显示"
    return body or "(空文件)"


@registry.tool(
    "write", "写入文件（覆盖已有内容），自动创建父目录。",
    {"type": "object",
     "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
     "required": ["path", "content"]},
)
def run_write(path: str, content: str) -> str:
    f = safe_path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字节）"


@registry.tool(
    "edit", "把文件中某段精确文本替换成新文本（只替换第一处）。",
    {"type": "object",
     "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                    "new_text": {"type": "string"}},
     "required": ["path", "old_text", "new_text"]},
)
def run_edit(path: str, old_text: str, new_text: str) -> str:
    f = safe_path(path)
    text = f.read_text(encoding="utf-8")
    if old_text not in text:
        # 失败信息要**可行动**：告诉模型该怎么办，而不只是说失败了。
        return f"错误：在 {path} 中找不到该文本。先用 read 确认当前内容。"
    if text.count(old_text) > 1:
        return f"错误：该文本在 {path} 中出现 {text.count(old_text)} 次，不唯一。请提供更长的上下文。"
    f.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已编辑 {path}"


@registry.tool(
    "glob", "按通配符查找文件，例如 '**/*.py'。",
    {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
)
def run_glob(pattern: str) -> str:
    hits = sorted(m for m in globlib.glob(pattern, root_dir=WORKSPACE, recursive=True))
    return "\n".join(hits) if hits else "(无匹配)"


@registry.tool(
    "grep", "在工作区内按子串搜索文件内容，返回 文件:行号:内容。",
    {"type": "object",
     "properties": {"pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "限定文件范围，默认 **/*"}},
     "required": ["pattern"]},
)
def run_grep(pattern: str, glob: str = "**/*") -> str:
    hits: list[str] = []
    for name in sorted(globlib.glob(glob, root_dir=WORKSPACE, recursive=True)):
        f = WORKSPACE / name
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if pattern in line:
                    hits.append(f"{name}:{i}:{line.strip()[:200]}")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制文件跳过，不要污染模型上下文
        if len(hits) >= 100:
            hits.append("… 结果过多，已截断")
            break
    return "\n".join(hits) if hits else "(无匹配)"


# ══════════════════════════════════════════════════════════════════
# Agent Loop —— 相比 s02 只改了两行
# ══════════════════════════════════════════════════════════════════


def agent_loop(provider, messages: list[dict], executor: ToolExecutor, system: str) -> str:
    """循环本身**一点没变**。

    变的只有两处：
      · tools=TOOLS            → tools=executor.registry.schemas()
      · if/elif 手动分派       → executor.execute(...)

    这件事本身就是一个结论：**好的 Harness 抽象不会改变 loop 的形状。**
    如果你加一个机制需要重写 agent_loop，八成是挂载点选错了。
    s13 会把这句话变成一条硬约束。
    """
    for _ in range(MAX_STEPS):
        reply = provider.chat(messages, tools=executor.registry.schemas(), system=system)
        messages.append(reply.as_assistant_message())

        if not reply.wants_tools:
            return reply.text

        for call in reply.tool_calls:
            print(f"  \033[33m→ {call.name}\033[0m \033[90m{_brief(call.arguments)}\033[0m")
            result = executor.execute(call.name, call.arguments)
            mark = "\033[31m✗\033[0m" if result.is_error else "\033[32m✓\033[0m"
            print(f"    {mark} \033[90m{result.content[:160].splitlines()[0] if result.content else ''}\033[0m")

            messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})

    return f"（达到 {MAX_STEPS} 步上限，停止）"


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:40]!r}" for k, v in args.items())


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def make_system(cwd: Path, reg: ToolRegistry) -> str:
    return (
        f"你是一个编程 Agent，工作目录是 {cwd}。\n"
        f"可用工具：{', '.join(reg.names())}。\n"
        "读文件优先用 read 而不是 bash cat。直接动手，不要解释你打算怎么做。"
    )


def build_demo_workspace() -> Path:
    d = Path(tempfile.mkdtemp(prefix="s03_demo_"))
    (d / "hello.py").write_text('def greet(name):\n    return "hi " + name\n\nprint(greet("world"))\n', encoding="utf-8")
    (d / "README.md").write_text("# demo\n\nTODO: 把 greet 改成中文问候\n", encoding="utf-8")
    return d


DEMO_SCRIPT = [
    scripted(calls=[("glob", {"pattern": "**/*.py"})]),
    scripted(calls=[("read", {"path": "hello.py"})]),
    scripted(calls=[("grep", {"pattern": "TODO"})]),
    scripted(calls=[("edit", {"path": "hello.py", "old_text": '"hi "', "new_text": '"你好，"'})]),
    scripted(calls=[("bash", {"command": "python3 hello.py"})]),
    # 故意演示三种错误路径：不存在的工具 / 缺参数 / 越界路径
    scripted(calls=[("teleport", {"to": "mars"})]),
    scripted(calls=[("edit", {"path": "hello.py"})]),
    scripted(calls=[("read", {"path": "../../../etc/passwd"})]),
    scripted("已把 greet 改成中文问候，运行输出「你好，world」。另外三次调用分别演示了未知工具、缺参数和路径越界的处理。"),
]


def main() -> None:
    global WORKSPACE
    demo = "--demo" in sys.argv
    executor = ToolExecutor(registry)

    print("\033[1ms03 — Tool Registry\033[0m")
    print(f"\033[90m注册了 {len(registry.names())} 个工具：{', '.join(registry.names())}\033[0m")

    if demo:
        WORKSPACE = build_demo_workspace()
        provider = get_provider(demo_script=DEMO_SCRIPT)
        print(f"\033[90m[demo] 离线假模型，工作区 {WORKSPACE}\033[0m\n")

        q = "看看这个项目，把 TODO 里说的事做了，然后跑一下验证。"
        print(f"\033[36m你 > \033[0m{q}")
        messages = [{"role": "user", "content": q}]
        answer = agent_loop(provider, messages, executor, make_system(WORKSPACE, registry))
        print(f"\033[32m模型 >\033[0m {answer}\n")

        print("\033[90m" + "─" * 62)
        print("模型看到的行动空间（registry.schemas() 的名字）：")
        print("  " + ", ".join(t["name"] for t in registry.schemas()))
        print("三条错误路径都变成了模型能读的字符串，没有一个异常冒出来：")
        print("  · 未知工具 teleport   → 列出了可用工具")
        print("  · edit 缺 old_text    → 指出了缺哪个参数")
        print("  · 读 ../../etc/passwd → 路径越界被拒")
        print("这就是 ToolExecutor 存在的意义：共享逻辑只写一遍。\033[0m")
        return

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return
    print(f"\033[90mprovider={provider.name} model={provider.model} workspace={WORKSPACE}\033[0m")
    print("\033[31m注意：bash 会真的执行，且此章仍无权限控制。建议在临时目录里跑。\033[0m")
    print("输入问题回车发送，q 退出。\n")

    messages: list[dict] = []
    system = make_system(WORKSPACE, registry)
    while True:
        try:
            q = input("\033[36m你 > \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if q.lower() in ("q", "quit", "exit", ""):
            return
        messages.append({"role": "user", "content": q})
        try:
            print(f"\033[32m模型 >\033[0m {agent_loop(provider, messages, executor, system)}\n")
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            return


if __name__ == "__main__":
    main()
