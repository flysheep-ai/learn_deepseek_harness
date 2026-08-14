#!/usr/bin/env python3
"""s18 — Full Harness（整合与验收）

                    User
                     │
                     ▼
                   Agent
                     │
              ┌──────┴──────┐
              ▼             ▼
          Session         Context
              │
              ▼
          Event Log
              │
              ▼
          Agent Loop
              │
              ▼
        Prompt Assembly
              │
              ▼
             LLM
              │
              ▼
          Tool Calls
              │
              ▼
    Tool Execution Pipeline
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
  Policy   Events   Tool Registry
              │
              ▼
     Capability Providers
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
   FS       Shell     Sandbox

这一章不新增任何机制。它做两件事：

  1. 把 s01–s17 组装成一台完整的机器（就是你正在读的这个文件）
  2. 用一个真实任务验收 —— 正是课程开头立下的那个标准：

     "帮我检查这个项目为什么测试失败，并修复它。"

     Harness 从头到尾不知道这是一个 debugging task。
     没有 run_test() / analyze_error() / modify_code() 的硬编码步骤，
     只有 read / write / edit / grep / glob / bash，
     以及 context / state / permission / session。

运行：
    python s18_full_harness/code.py --demo
    python s18_full_harness/code.py --demo --debug
    python s18_full_harness/code.py            # 真实模型，完整验收
"""

import glob as globlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

MAX_STEPS_PER_TURN = 16
CONTEXT_LIMIT_TOKENS = 4000
COMPACT_TRIGGER = 0.75
KEEP_RECENT_RATIO = 0.35


# ══════════════════════════════════════════════════════════════════
# 沿用 s05–s10（未改动）：事件日志
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SessionEvent:
    seq: int
    type: str
    data: dict[str, Any]
    time: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"seq": self.seq, "type": self.type, "time": self.time, "data": self.data},
                          ensure_ascii=False)

    @staticmethod
    def from_json(line: str) -> "SessionEvent":
        d = json.loads(line)
        return SessionEvent(d["seq"], d["type"], d["data"], d.get("time", 0.0))


EV_SESSION_START = "session/start"
EV_USER_MESSAGE = "user/message"
EV_ASSISTANT_MESSAGE = "assistant/message"
EV_TOOL_CALL = "tool/call"
EV_TOOL_RESULT = "tool/result"
EV_PERMISSION = "permission/decision"
EV_USAGE = "request/usage"
EV_TURN_START = "turn/start"
EV_TURN_END = "turn/end"
EV_STEP_START = "step/start"
EV_STEP_END = "step/end"
EV_REQUEST_HEADER = "request/header"
EV_SKILL_LOAD = "skill/load"
EV_SUBAGENT_START = "subagent/start"
EV_SUBAGENT_END = "subagent/end"
EV_COMPACTION_START = "compaction/start"
EV_COMPACTION_SUMMARY = "compaction/summary"
EV_COMPACTION_END = "compaction/end"
EV_TASK_WRITE = "task/write"
EV_JOB_START = "job/start"
EV_JOB_END = "job/end"
EV_PLUGIN_LOADED = "plugin/loaded"       # s14 新增
EV_PLUGIN_UNLOADED = "plugin/unloaded"   # s14 新增
EV_GOAL_START = "goal/start"             # s17 新增（log-only）
EV_GOAL_EVALUATE = "goal/evaluate"       # s17 新增（log-only）
EV_GOAL_BLOCKED = "goal/blocked"         # s17 新增（log-only）
EV_GOAL_COMPLETE = "goal/complete"       # s17 新增（log-only）

SURFACE_EVENTS = {EV_USER_MESSAGE, EV_ASSISTANT_MESSAGE, EV_TOOL_RESULT}


class Session:
    def __init__(self, session_id: str | None = None, path: Path | None = None) -> None:
        self.id = session_id or f"ses_{uuid.uuid4().hex[:10]}"
        self.path = path
        self._events: list[SessionEvent] = []
        self._seq = 0

    def append(self, type_: str, data: dict[str, Any]) -> SessionEvent:
        json.dumps(data, ensure_ascii=False)
        self._seq += 1
        ev = SessionEvent(self._seq, type_, data)
        self._events.append(ev)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(ev.to_json() + "\n")
        return ev

    def events(self, upto: int | None = None) -> list[SessionEvent]:
        return [e for e in self._events if upto is None or e.seq <= upto]

    def __len__(self) -> int:
        return len(self._events)

    def last_turn(self) -> int:
        return max((e.data["turn"] for e in self._events if e.type == EV_TURN_START), default=0)

    @classmethod
    def load(cls, path: Path) -> "Session":
        s = cls(session_id=path.stem, path=path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ev = SessionEvent.from_json(line)
                s._events.append(ev)
                s._seq = max(s._seq, ev.seq)
        return s


def collect_shadows(session: Session, upto: int | None = None) -> tuple[dict[int, str], set[int]]:
    events = session.events(upto)
    superseded: set[int] = set()
    for ev in events:
        if ev.type == EV_COMPACTION_SUMMARY:
            superseded.update(ev.data.get("supersedes", []))
    anchors: dict[int, str] = {}
    shadowed: set[int] = set()
    for ev in events:
        if ev.type != EV_COMPACTION_SUMMARY or ev.seq in superseded:
            continue
        seqs = ev.data["shadowed_seqs"]
        if seqs:
            anchors[min(seqs)] = ev.data["summary"]
            shadowed.update(seqs)
    return anchors, shadowed


def derive_messages(session: Session, upto: int | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    anchors, shadowed = collect_shadows(session, upto)
    for ev in session.events(upto):
        if ev.seq in anchors:
            messages.append({"role": "user", "content": anchors[ev.seq]})
        if ev.seq in shadowed or ev.type not in SURFACE_EVENTS:
            continue
        if ev.type == EV_USER_MESSAGE:
            messages.append({"role": "user", "content": ev.data["content"]})
        elif ev.type == EV_ASSISTANT_MESSAGE:
            msg: dict[str, Any] = {"role": "assistant", "content": ev.data.get("text", "")}
            if ev.data.get("tool_calls"):
                msg["tool_calls"] = ev.data["tool_calls"]
            messages.append(msg)
        elif ev.type == EV_TOOL_RESULT:
            messages.append({"role": "tool", "tool_call_id": ev.data["call_id"],
                             "content": ev.data["content"]})
    return messages


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        n += len(str(m.get("content", ""))) // 4
        for c in m.get("tool_calls") or []:
            n += len(json.dumps(c, ensure_ascii=False)) // 4
    return n


# ══════════════════════════════════════════════════════════════════
# 沿用 s06（未改动）：Inbox
# ══════════════════════════════════════════════════════════════════


@dataclass
class InboxItem:
    content: str
    source: str = "user"


class Inbox:
    def __init__(self) -> None:
        self._q: deque[InboxItem] = deque()

    def put(self, content: str, source: str = "user") -> None:
        self._q.append(InboxItem(content, source))

    def claim(self) -> list[InboxItem]:
        items = list(self._q)
        self._q.clear()
        return items

    def __bool__(self) -> bool:
        return bool(self._q)


# ══════════════════════════════════════════════════════════════════
# 沿用 s13（未改动）：EventBus
# ══════════════════════════════════════════════════════════════════


class EventBus:
    def __init__(self) -> None:
        self._observers: dict[str, list[tuple[int, str, Callable]]] = {}
        self._middleware: dict[str, list[tuple[int, str, Callable]]] = {}

    def on(self, event: str, fn: Callable, order: int = 100, owner: str = "") -> Callable[[], None]:
        self._observers.setdefault(event, []).append((order, owner, fn))
        return lambda: self._remove(self._observers, event, fn)

    def use(self, event: str, fn: Callable, order: int = 100, owner: str = "") -> Callable[[], None]:
        self._middleware.setdefault(event, []).append((order, owner, fn))
        return lambda: self._remove(self._middleware, event, fn)

    @staticmethod
    def _remove(table: dict, event: str, fn: Callable) -> None:
        table[event] = [e for e in table.get(event, []) if e[2] is not fn]

    def emit(self, event: str, *args: Any) -> None:
        for _, _, fn in sorted(self._observers.get(event, []), key=lambda e: e[0]):
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                print(f"\033[31m[bus] 观察者 {event} 抛异常：{type(e).__name__}: {e}\033[0m")

    def waterfall(self, event: str, ctx: Any, terminal: Callable[[], None] = lambda: None) -> None:
        chain = sorted(self._middleware.get(event, []), key=lambda e: e[0])

        def step(i: int) -> None:
            if i >= len(chain):
                terminal()
                return
            chain[i][2](ctx, lambda: step(i + 1))

        step(0)

    def describe(self) -> list[tuple[str, str, list[str]]]:
        out = []
        for event in sorted(set(self._observers) | set(self._middleware)):
            for kind, table in (("emit", self._observers), ("waterfall", self._middleware)):
                items = sorted(table.get(event, []), key=lambda e: e[0])
                if items:
                    out.append((event, kind, [f"{o}:{owner or fn.__name__}" for o, owner, fn in items]))
        return out


EVT_TOOL_CALL = "tool/call"
EVT_TOOL_PRE = "tool/pre-execute"
EVT_TOOL_EXECUTE = "tool/execute"
EVT_TOOL_POST = "tool/post-execute"
EVT_TOOL_RESULT = "tool/result"
EVT_STEP_PRE = "agent/pre-step"
EVT_STEP_END = "agent/step-end"
EVT_TURN_END = "agent/turn-end"


# ══════════════════════════════════════════════════════════════════
# 沿用 s03/s07（未改动）：Tool / Prompt 注册表
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    @property
    def required(self) -> list[str]:
        return list(self.parameters.get("required", []))

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        """s14 改动：注册**返回撤销函数**。

        这是整章唯一的机械改动，但它把"注册"从单向操作变成了可逆 effect。
        没有它，插件就只能装不能卸。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool
        return lambda: self._tools.pop(tool.name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def restricted(self, allowed: list[str]) -> "ToolRegistry":
        sub = ToolRegistry()
        for name in allowed:
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"限制列表里有不存在的工具：{name}")
            sub._tools[name] = tool
        return sub


@dataclass(frozen=True)
class PromptSection:
    name: str
    order: int
    render: Callable[["RuntimeContext"], str | None]


class SystemPromptRegistry:
    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def register(self, section: PromptSection) -> Callable[[], None]:
        self._sections[section.name] = section
        return lambda: self._sections.pop(section.name, None)

    def names(self) -> list[str]:
        return [s.name for s in sorted(self._sections.values(), key=lambda s: s.order)]

    def assemble(self, ctx: "RuntimeContext") -> str:
        parts = []
        for sec in sorted(self._sections.values(), key=lambda s: s.order):
            text = sec.render(ctx)
            if text:
                parts.append(text.strip())
        return "\n\n".join(parts)

    def explain(self, ctx: "RuntimeContext") -> list[tuple[str, int]]:
        return [(s.name, len((s.render(ctx) or "").strip()))
                for s in sorted(self._sections.values(), key=lambda s: s.order)]


@dataclass
class RuntimeContext:
    """s14 改动：它不再有 skills / tasks / jobs 这些**功能专属**字段。

    s07–s12 每加一个功能就往这里加一个字段，等于每个功能都在
    修改一个共享的数据结构 —— 那和"每加一个功能就改 ToolExecutor"
    是同一个病。

    现在功能自带状态（放在自己的 service 里），prompt section 直接
    从 service 读。RuntimeContext 只留**所有功能都需要**的东西。
    """

    cwd: Path
    tool_names: list[str] = field(default_factory=list)
    turn: int = 0
    step: int = 0
    project_notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)   # 功能自己的临时状态


# ══════════════════════════════════════════════════════════════════
# s15 新增：Capability Seams
# ══════════════════════════════════════════════════════════════════


@dataclass
class FileEntry:
    """文件系统返回的条目。纯数据，可 JSON 序列化 ——
    远程 provider 能通过网络把它传回来。"""
    path: str
    content: str
    is_dir: bool = False


@dataclass
class ShellResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0


class FileSystem(Protocol):
    """Service Definition：文件系统的接口。

    这是 seam 的**角色一**。它不 import 任何 provider，
    只是声明"一个文件系统能做什么"。

    刻意保持很小 —— 只有工具真正需要的六个方法。
    接口每多一个方法，所有 provider 都要实现它一遍。
    """

    def read(self, path: str, limit_lines: int | None = None) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    def edit(self, path: str, old_text: str, new_text: str) -> str: ...
    def glob(self, pattern: str) -> list[str]: ...
    def grep(self, pattern: str, file_glob: str) -> list[str]: ...
    def exists(self, path: str) -> bool: ...


class Shell(Protocol):
    """Service Definition：shell 执行的接口。"""

    def run(self, command: str, cwd: str, timeout: float) -> ShellResult: ...


class LocalFileSystem:
    """Provider（角色二）：真实磁盘。

    所有越界检查在这里做一次，而不是在每个工具里做 ——
    换 provider 时不需要担心"某个工具忘了检查"。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"路径越界，超出工作区：{path}")
        return p

    def read(self, path: str, limit_lines: int | None = None) -> str:
        lines = self._resolve(path).read_text(encoding="utf-8").splitlines()
        shown = lines[:limit_lines] if limit_lines else lines
        body = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(shown, 1))
        if limit_lines and len(lines) > limit_lines:
            body += f"\n… 还有 {len(lines) - limit_lines} 行未显示"
        return body or "(空文件)"

    def write(self, path: str, content: str) -> None:
        f = self._resolve(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        f = self._resolve(path)
        text = f.read_text(encoding="utf-8")
        if old_text not in text:
            return f"错误：在 {path} 中找不到该文本。先用 read 确认当前内容。"
        if text.count(old_text) > 1:
            return f"错误：该文本在 {path} 中出现 {text.count(old_text)} 次，不唯一。请提供更长的上下文。"
        f.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"

    def glob(self, pattern: str) -> list[str]:
        return sorted(globlib.glob(pattern, root_dir=self.root, recursive=True))

    def grep(self, pattern: str, file_glob: str) -> list[str]:
        hits: list[str] = []
        for name in sorted(globlib.glob(file_glob, root_dir=self.root, recursive=True)):
            f = self.root / name
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern in line:
                        hits.append(f"{name}:{i}:{line.strip()[:200]}")
            except (UnicodeDecodeError, OSError):
                continue
        return hits[:100]

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()


class MemoryFileSystem:
    """Provider（角色二，第二个）：纯内存。

    它和 LocalFileSystem 有**完全相同的接口**，但没有任何磁盘 I/O。
    价值有两个：
      · 测试    —— 不确定性的工具测试跑在内存里，秒级、无副作用
      · 教学    —— 它证明了"换 provider = 换世界"，而且是零成本的
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.dirs: set[str] = {""}

    def _check(self, path: str) -> str:
        p = Path(path)
        if any(part in ("..", "") and i > 0 and part == ".." for i, part in enumerate(p.parts)):
            pass
        if ".." in p.parts:
            raise ValueError(f"路径越界：{path}")
        return str(p).replace("\\", "/").lstrip("./")

    def _read_raw(self, path: str) -> str:
        return self.files.get(self._check(path), "")

    def read(self, path: str, limit_lines: int | None = None) -> str:
        content = self._read_raw(path)
        lines = content.splitlines()
        shown = lines[:limit_lines] if limit_lines else lines
        body = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(shown, 1))
        if limit_lines and len(lines) > limit_lines:
            body += f"\n… 还有 {len(lines) - limit_lines} 行未显示"
        return body or "(空文件)"

    def write(self, path: str, content: str) -> None:
        p = self._check(path)
        # 维护目录集合，供 glob 用
        parts = Path(p).parts
        for i in range(len(parts)):
            self.dirs.add("/".join(parts[:i]))
        self.files[p] = content

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        p = self._check(path)
        text = self._read_raw(p)
        if old_text not in text:
            return f"错误：在 {path} 中找不到该文本。先用 read 确认当前内容。"
        if text.count(old_text) > 1:
            return f"错误：该文本在 {path} 中出现 {text.count(old_text)} 次，不唯一。"
        self.files[p] = text.replace(old_text, new_text, 1)
        return f"已编辑 {path}"

    def glob(self, pattern: str) -> list[str]:
        import fnmatch
        out = []
        for p in sorted(self.files):
            if fnmatch.fnmatch(p, pattern):
                out.append(p)
        # 也支持 */ 目录前缀匹配
        for p in sorted(self.files):
            for prefix in sorted(self.dirs):
                if prefix and fnmatch.fnmatch(prefix + "/" + p, pattern):
                    if prefix + "/" + p not in out:
                        out.append(prefix + "/" + p)
        return out

    def grep(self, pattern: str, file_glob: str) -> list[str]:
        import fnmatch
        # fnmatch 的 * 不跨目录，所以先把 ** 展开成 {*/*, *} 两层，
        # 让 "**/*" 同时匹配顶层文件和一层子目录。真实实现会用更完整的
        # glob 语义，这里够教学用。
        patterns = [file_glob] if "**" not in file_glob else [file_glob, file_glob.replace("**/", "*/*"), "*"]
        hits: list[str] = []
        for p in sorted(self.files):
            if not any(fnmatch.fnmatch(p, pat) for pat in patterns):
                continue
            for i, line in enumerate(self.files[p].splitlines(), 1):
                if pattern in line:
                    hits.append(f"{p}:{i}:{line.strip()[:200]}")
        return hits[:100]

    def exists(self, path: str) -> bool:
        return self._check(path) in self.files


class LocalShell:
    """Provider：本机 shell。"""

    def run(self, command: str, cwd: str, timeout: float = 60.0) -> ShellResult:
        try:
            r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout)
            return ShellResult(stdout=r.stdout, stderr=r.stderr, exit_code=r.returncode)
        except subprocess.TimeoutExpired:
            return ShellResult(stdout="", stderr=f"错误：命令超时（{timeout:.0f} 秒）。"
                                                "耗时长的命令请用 bash_background。", exit_code=124)


class DryRunShell:
    """Provider：只打印、不执行。

    又一个同接口的 provider —— 它是"预览模式"的最小实现。
    真实系统里的 sandbox provider 也是这么接的：接口相同，世界不同。
    """

    def __init__(self) -> None:
        self.history: list[str] = []

    def run(self, command: str, cwd: str, timeout: float = 60.0) -> ShellResult:
        self.history.append(command)
        return ShellResult(stdout=f"[dry-run] 本应执行：{command}\n（此 provider 不会真的执行任何命令）",
                           stderr="", exit_code=0)


SEAM_KEYS = ("fs", "shell")


class CapabilityPlugin:
    """把 provider 装进 harness 的插件。

    它 provide 两个 service：fs 和 shell。**用哪个 provider，
    在构造插件时决定，其余全部代码对此无感。**
    """

    name = "capabilities"

    def __init__(self, fs: FileSystem, shell: Shell) -> None:
        self.fs = fs
        self.shell = shell

    def setup(self, ctx) -> None:
        ctx.provide("fs", self.fs)
        ctx.provide("shell", self.shell)


# ══════════════════════════════════════════════════════════════════
# s14 新增：Plugin / PluginContext / Harness
# ══════════════════════════════════════════════════════════════════


class Plugin(Protocol):
    """一个插件。

    只要求两样东西：一个名字，一个 setup。

    **没有 teardown 方法** —— 这是刻意的。
    teardown 靠 PluginContext 自动收集的撤销函数完成，
    而不是靠插件作者手写一个和 setup 对称的清理函数。

    为什么？因为手写清理**一定会漏**。setup 里加了一行注册，
    teardown 里忘了加对应的一行 —— 这种 bug 静默、难查、且必然发生。
    让注册这个动作本身返回撤销函数，漏不掉。
    """

    name: str

    def setup(self, ctx: "PluginContext") -> None: ...


class PluginContext:
    """插件能看到的世界。

    插件**不能**直接碰 harness 的内部结构，只能通过这几个方法：

        tool()      注册工具
        section()   注册 prompt 段
        on()/use()  注册监听器
        provide()   提供一个 service（供别的插件按 key 取用）
        require()   取用别的插件提供的 service

    每一个都返回撤销函数，并被自动记进 self._disposers。
    卸载时**逆序**执行 —— 顺序反过来，是因为后注册的可能依赖先注册的。
    """

    def __init__(self, harness: "Harness", plugin_name: str) -> None:
        self.harness = harness
        self.plugin_name = plugin_name
        self._disposers: list[Callable[[], None]] = []

    # ── 注册（全部可逆）───────────────────────────────────────────
    def tool(self, name: str, description: str, parameters: dict[str, Any]) -> Callable:
        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            off = self.harness.tools.register(Tool(name, description, parameters, fn))
            self._disposers.append(off)
            return fn
        return deco

    def section(self, name: str, order: int) -> Callable:
        def deco(fn: Callable[[RuntimeContext], str | None]):
            off = self.harness.prompts.register(PromptSection(name, order, fn))
            self._disposers.append(off)
            return fn
        return deco

    # on/use 既能直接调用，也能当装饰器 —— 插件里写成装饰器可读性更好：
    #     @ctx.on(EVT_TOOL_RESULT, order=10)
    #     def _log(tc): ...
    def on(self, event: str, fn: Callable | None = None, order: int = 100):
        if fn is None:
            return lambda f: (self.on(event, f, order), f)[1]
        self._disposers.append(self.harness.bus.on(event, fn, order, owner=self.plugin_name))
        return fn

    def use(self, event: str, fn: Callable | None = None, order: int = 100):
        if fn is None:
            return lambda f: (self.use(event, f, order), f)[1]
        self._disposers.append(self.harness.bus.use(event, fn, order, owner=self.plugin_name))
        return fn

    def provide(self, key: str, service: Any) -> None:
        """提供一个 service。

        service 按 **key** 取用，不按类型、不靠 import。
        这样"谁实现了 tasks"这件事在运行时才决定，
        s15 会把这个想法推到底。
        """
        if key in self.harness.services:
            raise ValueError(f"service '{key}' 已被 {self.harness.service_owner[key]} 提供")
        self.harness.services[key] = service
        self.harness.service_owner[key] = self.plugin_name

        def off() -> None:
            self.harness.services.pop(key, None)
            self.harness.service_owner.pop(key, None)
        self._disposers.append(off)

    def require(self, key: str) -> Any:
        svc = self.harness.services.get(key)
        if svc is None:
            # 早失败，且说清楚缺谁。
            # 不做"自动排序依赖"这种魔法：显式的加载顺序比隐式的推导好读。
            raise RuntimeError(
                f"插件 {self.plugin_name} 需要 service '{key}'，但没有插件提供它。"
                f"当前已有：{', '.join(self.harness.services) or '（无）'}")
        return svc

    # ── 只读地看到宿主 ───────────────────────────────────────────
    @property
    def session(self) -> Session:
        return self.harness.session

    @property
    def inbox(self) -> Inbox:
        return self.harness.inbox

    @property
    def rt(self) -> RuntimeContext:
        return self.harness.rt

    def unload(self) -> None:
        for off in reversed(self._disposers):     # 逆序
            off()
        self._disposers.clear()


class Harness:
    """把插件装配起来的宿主。

    它自己**什么功能都没有** —— 没有工具、没有 prompt、没有监听器。
    bash 是插件给的，权限是插件给的，连"把工具调用写进日志"都是插件给的。

        There is no privileged core to patch.

    这句话的实际意义是：你想改任何行为，都不需要修改这个类。
    """

    def __init__(self, session: Session, cwd: Path) -> None:
        self.session = session
        self.bus = EventBus()
        self.tools = ToolRegistry()
        self.prompts = SystemPromptRegistry()
        self.inbox = Inbox()
        self.rt = RuntimeContext(cwd=cwd)
        self.services: dict[str, Any] = {}
        self.service_owner: dict[str, str] = {}
        self._loaded: dict[str, PluginContext] = {}
        self._order: list[str] = []

    def use(self, plugin: Plugin) -> "Harness":
        if plugin.name in self._loaded:
            raise ValueError(f"插件重复加载：{plugin.name}")
        pctx = PluginContext(self, plugin.name)
        plugin.setup(pctx)
        self._loaded[plugin.name] = pctx
        self._order.append(plugin.name)
        self.session.append(EV_PLUGIN_LOADED, {"plugin": plugin.name})
        return self                                    # 链式：harness.use(A).use(B)

    def unload(self, name: str) -> None:
        pctx = self._loaded.pop(name, None)
        if pctx is None:
            return
        pctx.unload()
        self._order.remove(name)
        self.session.append(EV_PLUGIN_UNLOADED, {"plugin": name})

    def loaded(self) -> list[str]:
        return list(self._order)

    def get(self, key: str) -> Any:
        return self.services.get(key)


# ══════════════════════════════════════════════════════════════════
# 沿用 s04（未改动）：权限策略
# ══════════════════════════════════════════════════════════════════


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


DENY_PATTERNS = [
    (r"\brm\s+(-\w+\s+)*-\w*[rf]\w*\s+(/|~|\$HOME)(\s|$)", "递归删除根目录或家目录"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork 炸弹"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "关机/重启"),
    (r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh", "把远程脚本直接管进 shell"),
]
SAFE_BASH = re.compile(
    r"^\s*(ls|pwd|cat|head|tail|wc|file|stat|find|grep|rg|which|echo|date|"
    r"git\s+(status|log|diff|show|branch)|pytest|python3?\s+-m\s+pytest|"
    r"python3?\s+-c\s+|python3?\s+--version|uname|env|df|du)\b"
)


class PermissionPolicy:
    def __init__(self, yolo: bool = False, read_only: bool = False) -> None:
        self.yolo = yolo
        self.read_only = read_only

    def check(self, name: str, args: dict[str, Any]) -> Verdict:
        if name in ("read", "glob", "grep", "skill", "job_status", "job_output"):
            return Verdict(Decision.ALLOW, "只读操作")
        if self.read_only:
            return Verdict(Decision.DENY, "当前处于只读模式")
        if self.yolo:
            return Verdict(Decision.ALLOW, "yolo 模式")
        if name in ("write", "edit"):
            return Verdict(Decision.ASK, f"将修改文件 {args.get('path', '?')}")
        if name in ("bash", "bash_background"):
            cmd = str(args.get("command", ""))
            for pattern, why in DENY_PATTERNS:
                if re.search(pattern, cmd):
                    return Verdict(Decision.DENY, why)
            if SAFE_BASH.match(cmd):
                return Verdict(Decision.ALLOW, "只读命令")
            return Verdict(Decision.ASK, "将执行 shell 命令")
        return Verdict(Decision.ASK, "未定义规则的工具")


Approver = Callable[[str, dict[str, Any], str], bool]


def cli_approver(name: str, args: dict[str, Any], reason: str) -> bool:
    print(f"\n  \033[35m[需要批准]\033[0m {name} — {reason}")
    for k, v in args.items():
        print(f"    {k} = {str(v)[:300]}")
    try:
        return input("  批准执行？[y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ══════════════════════════════════════════════════════════════════
# 沿用 s13（未改动）：Tracer / ToolExecutor
# ══════════════════════════════════════════════════════════════════


class Tracer:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def turn_start(self, turn: int) -> None:
        if self.enabled:
            print(f"\033[1;34m[turn {turn} start]\033[0m")

    def turn_end(self, turn: int, reason: str, steps: int) -> None:
        if self.enabled:
            print(f"\033[1;34m[turn {turn} end]\033[0m reason={reason} steps={steps}\n")

    def step_start(self, turn: int, step: int, claimed: list[InboxItem]) -> None:
        if self.enabled:
            src = f" ({claimed[0].source})" if claimed else ""
            print(f"  \033[34m[step {step}]\033[0m  claimed={len(claimed)}{src}")

    def request(self, messages: list, tools: list, system: str) -> None:
        if self.enabled:
            tok = estimate_tokens(messages)
            print(f"    \033[90m→ model request   messages={len(messages)} tools={len(tools)} "
                  f"system={len(system)}chars  ctx={tok}/{CONTEXT_LIMIT_TOKENS}\033[0m")

    def reply(self, reply) -> None:
        if self.enabled:
            names = ",".join(c.name for c in reply.tool_calls) or "-"
            print(f"    \033[90m← model reply     text={len(reply.text)}chars "
                  f"tool_calls={len(reply.tool_calls)} [{names}]\033[0m")

    def tool_pre(self, name: str, args: dict, verdict: str) -> None:
        if self.enabled:
            print(f"    \033[90m· tool pre        {name} {_brief(args)[:56]} → {verdict}\033[0m")

    def tool_result(self, name: str, result: ToolResult) -> None:
        if self.enabled:
            state = "error" if result.is_error else "ok"
            print(f"    \033[90m· tool result     {name} {state} {len(result.content)}B\033[0m")

    def step_end(self, turn: int, step: int) -> None:
        if self.enabled:
            print(f"  \033[34m[step {step} end]\033[0m")


@dataclass
class ToolCallCtx:
    call_id: str
    name: str
    arguments: dict[str, Any]
    turn: int
    step: int
    session: Session
    registry: ToolRegistry
    result: ToolResult | None = None
    verdict: Verdict | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, bus: EventBus) -> None:
        self.registry = registry
        self.bus = bus

    def run_body(self, ctx: ToolCallCtx) -> None:
        tool = ctx.registry.get(ctx.name)
        assert tool is not None
        known = set(tool.parameters.get("properties", {}))
        cleaned = {k: v for k, v in ctx.arguments.items() if k in known}
        try:
            ctx.result = ToolResult(tool.handler(**cleaned))
        except Exception as e:  # noqa: BLE001
            ctx.result = ToolResult(f"错误：{type(e).__name__}: {e}", is_error=True)

    def execute(self, call_id: str, name: str, arguments: dict[str, Any],
                session: Session, turn: int, step: int) -> ToolResult:
        ctx = ToolCallCtx(call_id, name, arguments, turn, step, session, self.registry)
        self.bus.emit(EVT_TOOL_CALL, ctx)
        self.bus.waterfall(EVT_TOOL_PRE, ctx)
        if ctx.result is None:
            self.bus.waterfall(EVT_TOOL_EXECUTE, ctx, terminal=lambda: self.run_body(ctx))
        self.bus.waterfall(EVT_TOOL_POST, ctx)
        self.bus.emit(EVT_TOOL_RESULT, ctx)
        return ctx.result or ToolResult("(工具没有产生结果)", is_error=True)


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:50]!r}" for k, v in args.items())


@dataclass
class StepPreCtx:
    turn: int
    step: int
    items: list[InboxItem]
    rejected: bool = False
    reject_reason: str = ""


@dataclass
class TurnOutcome:
    turn: int
    steps: int
    reason: str
    text: str


# ══════════════════════════════════════════════════════════════════
# Agent Loop —— 它现在**完全不知道**有哪些功能存在
# ══════════════════════════════════════════════════════════════════


def run_turn(provider, harness: Harness, executor: ToolExecutor, tracer: Tracer,
             prompt_registry: SystemPromptRegistry | None = None,
             rt: RuntimeContext | None = None,
             session: Session | None = None,
             inbox: Inbox | None = None) -> TurnOutcome:
    """把 s06–s13 的 run_turn 搬过来，只改了一件事：

        它不再直接引用 SKILLS / TASKS / JOBS / SUMMARIZER 这些全局变量。

    技能目录怎么进 prompt？SkillPlugin 注册的 section 自己去读。
    任务清单怎么刷新？TaskPlugin 注册的 section 自己去读。
    后台任务完成通知怎么注入？JobPlugin 挂在 agent/pre-step 上自己注入。
    上下文什么时候压缩？CompactionPlugin 挂在 agent/pre-step 上自己判断。

    结果：**卸载任何一个插件，这个函数都不用改。**
    """
    session = session or harness.session
    inbox = inbox or harness.inbox
    rt = rt or harness.rt
    pr = prompt_registry or harness.prompts
    # s16：成员 Agent 复用自己的 session/rt，但共享主 harness 的总线。
    bus = harness.bus if harness else None

    turn = session.last_turn() + 1
    session.append(EV_TURN_START, {"turn": turn})
    tracer.turn_start(turn)

    step = 0
    reason = "natural-stop"
    final_text = ""
    last_header: str | None = None
    rt.turn = turn

    while True:
        claimed = inbox.claim()

        # 让插件在认领之后、进入 step 之前插手（注入、压缩、拦截都挂在这里）
        pre = StepPreCtx(turn=turn, step=step + 1, items=list(claimed))
        if bus:
            bus.waterfall(EVT_STEP_PRE, pre)
        if pre.rejected:
            session.append(EV_TURN_END, {"turn": turn, "reason": "rejected",
                                         "steps": step, "why": pre.reject_reason})
            tracer.turn_end(turn, "rejected", step)
            return TurnOutcome(turn, step, "rejected", pre.reject_reason)
        claimed = pre.items
        # 插件可能在 pre-step 里往 inbox 又放了东西（比如 job 完成通知）
        claimed += inbox.claim()

        if step == 0 and not claimed:
            reason = "no-input"
            break

        step += 1
        session.append(EV_STEP_START, {"turn": turn, "step": step})
        tracer.step_start(turn, step, claimed)

        for item in claimed:
            session.append(EV_USER_MESSAGE, {"turn": turn, "step": step,
                                             "content": item.content, "source": item.source})

        rt.step = step
        rt.tool_names = executor.registry.names()
        system = pr.assemble(rt)
        messages = derive_messages(session)
        tools = executor.registry.schemas()
        tracer.request(messages, tools, system)

        if system != last_header:
            session.append(EV_REQUEST_HEADER, {
                "turn": turn, "step": step, "system": system,
                "tools": [t["name"] for t in tools],
                "sections": [n for n, size in pr.explain(rt) if size],
            })
            last_header = system

        reply = provider.chat(messages, tools=tools, system=system)
        tracer.reply(reply)

        session.append(EV_ASSISTANT_MESSAGE, {
            "turn": turn, "step": step, "text": reply.text,
            "tool_calls": reply.as_assistant_message().get("tool_calls", []),
        })
        if reply.usage:
            session.append(EV_USAGE, {"turn": turn, "step": step, **reply.usage})

        indent = "  " + ("    " if prompt_registry is not None else "")
        for call in reply.tool_calls:
            if not tracer.enabled:
                print(f"{indent}\033[33m→ {call.name}\033[0m \033[90m{_brief(call.arguments)}\033[0m")
            result = executor.execute(call.id, call.name, call.arguments, session, turn, step)
            if not tracer.enabled:
                mark = "\033[31m✗\033[0m" if result.is_error else "\033[32m✓\033[0m"
                first = result.content[:130].splitlines()[0] if result.content else ""
                print(f"{indent}  {mark} \033[90m{first}\033[0m")

        session.append(EV_STEP_END, {"turn": turn, "step": step})
        if bus:
            bus.emit(EVT_STEP_END, turn, step, session)
        tracer.step_end(turn, step)

        if reply.text:
            final_text = reply.text

        if reply.wants_tools or inbox:
            pass
        else:
            reason = "natural-stop"
            break
        if step >= MAX_STEPS_PER_TURN:
            reason = "max-steps"
            break

    session.append(EV_TURN_END, {"turn": turn, "reason": reason, "steps": step})
    if bus:
        bus.emit(EVT_TURN_END, turn, reason, step)
    tracer.turn_end(turn, reason, step)
    return TurnOutcome(turn, step, reason, final_text)


# ══════════════════════════════════════════════════════════════════
# 插件 ① 核心工具：文件系统与 shell
# ══════════════════════════════════════════════════════════════════


class CoreToolsPlugin:
    """文件与 shell 工具。它们全部是 seam 的 **Consumer**（角色三）。

    s14 里这些工具直接调 pathlib / subprocess —— handler 里写死了实现。
    现在它们只依赖 `fs` / `shell` 两个 service 的**接口**：

        read    → fs.read
        write   → fs.write
        edit    → fs.edit
        glob    → fs.glob
        grep    → fs.grep
        bash    → shell.run

    Consumer 不 import Provider。背后是本地磁盘还是内存还是远程沙箱，
    与它无关。这就是"换 provider，整个产品跟着换，工具一个字不改"。
    """

    name = "core-tools"

    def setup(self, ctx: PluginContext) -> None:
        fs = ctx.require("fs")
        shell = ctx.require("shell")

        @ctx.tool("bash", "在工作目录下执行一条 shell 命令。",
                  {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]})
        def _bash(command: str) -> str:
            r = shell.run(command, str(ctx.rt.cwd))
            out = (r.stdout + r.stderr).strip()
            if r.exit_code != 0:
                out = f"[exit {r.exit_code}]\n{out}"
            return out[:20000] if out else "(无输出)"

        @ctx.tool("read", "读取文件内容，返回带行号的文本。",
                  {"type": "object", "properties": {"path": {"type": "string"},
                                                    "limit": {"type": "integer"}},
                   "required": ["path"]})
        def _read(path: str, limit: int | None = None) -> str:
            return fs.read(path, limit_lines=limit)

        @ctx.tool("write", "写入文件（覆盖已有内容）。",
                  {"type": "object", "properties": {"path": {"type": "string"},
                                                    "content": {"type": "string"}},
                   "required": ["path", "content"]})
        def _write(path: str, content: str) -> str:
            fs.write(path, content)
            return f"已写入 {path}（{len(content)} 字节）"

        @ctx.tool("edit", "把文件中某段精确文本替换成新文本（只替换第一处）。",
                  {"type": "object", "properties": {"path": {"type": "string"},
                                                    "old_text": {"type": "string"},
                                                    "new_text": {"type": "string"}},
                   "required": ["path", "old_text", "new_text"]})
        def _edit(path: str, old_text: str, new_text: str) -> str:
            return fs.edit(path, old_text, new_text)

        @ctx.tool("glob", "按通配符查找文件，例如 '**/*.py'。",
                  {"type": "object", "properties": {"pattern": {"type": "string"}},
                   "required": ["pattern"]})
        def _glob(pattern: str) -> str:
            hits = fs.glob(pattern)
            return "\n".join(hits) if hits else "(无匹配)"

        @ctx.tool("grep", "在工作区内按子串搜索文件内容。",
                  {"type": "object", "properties": {"pattern": {"type": "string"},
                                                    "glob": {"type": "string"}},
                   "required": ["pattern"]})
        def _grep(pattern: str, glob: str = "**/*") -> str:
            hits = fs.grep(pattern, glob)
            return "\n".join(hits) if hits else "(无匹配)"

        @ctx.section("environment", 20)
        def _env(rt: RuntimeContext) -> str:
            return f"# 环境\n工作目录：{rt.cwd}\n所有文件操作都被限制在这个目录内。"

        @ctx.section("tools", 40)
        def _tools_section(rt: RuntimeContext) -> str:
            return (f"# 工具\n可用工具：{', '.join(rt.tool_names)}\n"
                    "读文件优先用 read 而不是 bash cat；查找用 glob/grep 而不是 find。")


# ══════════════════════════════════════════════════════════════════
# 插件 ② 会话日志 / 权限 / trace / 脱敏 / 计时（全部来自 s13）
# ══════════════════════════════════════════════════════════════════


class IdentityPlugin:
    """Agent 的人格。单独成一个插件，因为它是最常被替换的东西。"""

    name = "identity"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or "你是一个编程 Agent。直接动手完成任务，不要先解释你打算怎么做。"

    def setup(self, ctx) -> None:
        @ctx.section("identity", 10)
        def _identity(rt: RuntimeContext) -> str:
            return self.text

        @ctx.section("project", 30)
        def _project(rt: RuntimeContext) -> str | None:
            return f"# 项目约定\n{rt.project_notes.strip()}" if rt.project_notes else None


class SessionLogPlugin:
    """把工具调用写进事件日志。

    它是一个**插件** —— 意味着理论上你可以卸载它。
    卸载之后 Agent 照常工作，但日志里就没有 tool/call 和 tool/result 了，
    上下文里也就没有工具结果（因为 derive_messages 从日志投影）。

    这件事本身很能说明问题：**日志不是副作用，日志是主干。**
    """

    name = "session-log"

    def setup(self, ctx: PluginContext) -> None:
        @ctx.on(EVT_TOOL_CALL, order=10)
        def _log_call(tc: ToolCallCtx) -> None:
            tc.session.append(EV_TOOL_CALL, {"turn": tc.turn, "step": tc.step,
                                             "call_id": tc.call_id, "name": tc.name,
                                             "arguments": tc.arguments})

        @ctx.on(EVT_TOOL_RESULT, order=10)
        def _log_result(tc: ToolCallCtx) -> None:
            r = tc.result or ToolResult("")
            tc.session.append(EV_TOOL_RESULT, {"turn": tc.turn, "step": tc.step,
                                               "call_id": tc.call_id, "name": tc.name,
                                               "content": r.content, "is_error": r.is_error})


class ValidationPlugin:
    name = "validation"

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_TOOL_PRE, order=10)
        def _validate(tc: ToolCallCtx, next_: Callable) -> None:
            tool = tc.registry.get(tc.name)
            if tool is None:
                tc.result = ToolResult(
                    f"错误：没有名为 '{tc.name}' 的工具。可用工具：{', '.join(tc.registry.names())}",
                    is_error=True)
                return
            missing = [k for k in tool.required if k not in tc.arguments]
            if missing:
                tc.result = ToolResult(f"错误：{tc.name} 缺少必填参数：{', '.join(missing)}", is_error=True)
                return
            next_()


class PermissionPlugin:
    name = "permission"

    def __init__(self, policy: PermissionPolicy, approver: Approver) -> None:
        self.policy = policy
        self.approver = approver

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_TOOL_PRE, order=20)
        def _permission(tc: ToolCallCtx, next_: Callable) -> None:
            tc.verdict = self.policy.check(tc.name, tc.arguments)
            approved: bool | None = None
            if tc.verdict.decision is Decision.ASK:
                approved = self.approver(tc.name, tc.arguments, tc.verdict.reason)
            tc.session.append(EV_PERMISSION, {
                "turn": tc.turn, "step": tc.step, "call_id": tc.call_id, "tool": tc.name,
                "decision": tc.verdict.decision.value, "reason": tc.verdict.reason,
                "approved": approved})
            if tc.verdict.decision is Decision.DENY:
                tc.result = ToolResult(
                    f"权限拒绝：{tc.verdict.reason}。这个操作在本环境中被禁止，请换一种方式。",
                    is_error=True)
                return
            if approved is False:
                tc.result = ToolResult("用户拒绝了这次操作。请换一种方式，或者先说明你为什么需要它。",
                                       is_error=True)
                return
            next_()


class TracePlugin:
    name = "trace"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_TOOL_PRE, order=90)
        def _trace_pre(tc: ToolCallCtx, next_: Callable) -> None:
            next_()
            v = tc.verdict.decision.value if tc.verdict else ("denied" if tc.result else "ok")
            self.tracer.tool_pre(tc.name, tc.arguments, v)

        @ctx.on(EVT_TOOL_RESULT, order=90)
        def _trace_result(tc: ToolCallCtx) -> None:
            if tc.result:
                self.tracer.tool_result(tc.name, tc.result)


SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "sk-***"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s'\"]+)"), r"\1***"),
    (re.compile(r"(?i)(token\s*[=:]\s*)([^\s'\"]+)"), r"\1***"),
]


class RedactPlugin:
    name = "redact"

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_TOOL_POST, order=10)
        def _redact(tc: ToolCallCtx, next_: Callable) -> None:
            next_()
            r = tc.result
            if not r or not r.content:
                return
            text = r.content
            for pattern, repl in SECRET_PATTERNS:
                text = pattern.sub(repl, text)
            if text != r.content:
                tc.meta["redacted"] = True
                tc.result = ToolResult(text, r.is_error)


class TruncatePlugin:
    name = "truncate"

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_TOOL_POST, order=20)
        def _truncate(tc: ToolCallCtx, next_: Callable) -> None:
            next_()
            r = tc.result
            if r and len(r.content) > 20000:
                head, tail = r.content[:12000], r.content[-4000:]
                tc.result = ToolResult(
                    f"{head}\n\n…（省略 {len(r.content) - 16000} 字符）…\n\n{tail}", r.is_error)


class TimingPlugin:
    """统计工具耗时。它 provide 了一个 service，别人可以取用。"""

    name = "timing"

    def setup(self, ctx: PluginContext) -> None:
        stats: dict[str, list[float]] = {}
        ctx.provide("timing", stats)

        @ctx.use(EVT_TOOL_EXECUTE, order=10)
        def _timing(tc: ToolCallCtx, next_: Callable) -> None:
            t0 = time.perf_counter()
            try:
                next_()
            finally:
                stats.setdefault(tc.name, []).append((time.perf_counter() - t0) * 1000)


# ══════════════════════════════════════════════════════════════════
# 插件 ③ Skill（s08）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path

    @property
    def body(self) -> str:
        text = self.path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            text = text[end + 4:] if end >= 0 else text
        return text.strip()


class SkillRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._skills: dict[str, Skill] = {}
        self.discover()

    def discover(self) -> None:
        self._skills.clear()
        if not self.root.exists():
            return
        for f in sorted(self.root.glob("*/SKILL.md")):
            text = f.read_text(encoding="utf-8")
            meta: dict[str, str] = {}
            if text.startswith("---"):
                end = text.find("\n---", 3)
                for line in text[3:end if end > 0 else 0].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
            name = meta.get("name") or f.parent.name
            if meta.get("description"):
                self._skills[name] = Skill(name, meta["description"], f)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def catalog(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self._skills.values()]


class SkillPlugin:
    """技能：一个 service + 一个工具 + 一个 prompt 段。

    三样东西属于同一个功能，所以它们在同一个插件里。
    卸载它，三样一起消失 —— 这就是"功能有边界"的意思。
    """

    name = "skills"

    def __init__(self, root: Path) -> None:
        self.root = root

    def setup(self, ctx: PluginContext) -> None:
        skills = SkillRegistry(self.root)
        ctx.provide("skills", skills)
        loaded: list[str] = []

        @ctx.tool("skill", "加载一份技能的完整内容。只在你确实需要那份知识时调用。",
                  {"type": "object", "properties": {"name": {"type": "string"}},
                   "required": ["name"]})
        def _skill(name: str) -> str:
            s = skills.get(name)
            if s is None:
                return f"错误：没有名为 '{name}' 的技能。可用：{', '.join(skills.names())}"
            if name in loaded:
                return f"技能 {name} 已经加载过了，内容就在上文，不要重复加载。"
            loaded.append(name)
            body = s.body
            ctx.inbox.put(f"[已加载技能：{name}]\n\n{body}", source="skill")
            ctx.session.append(EV_SKILL_LOAD, {"name": name})
            return f"已加载技能 {name}（{len(body)} 字符），内容将在下一步进入上下文。"

        @ctx.section("skills", 45)
        def _skills_section(rt: RuntimeContext) -> str | None:
            cat = skills.catalog()
            if not cat:
                return None
            lines = [f"- {n}：{d}" for n, d in cat]
            tail = f"\n已加载：{', '.join(loaded)}" if loaded else ""
            return ("# 可用技能\n"
                    "下面是可按需加载的知识。**只列了标题**，需要时用 skill 工具加载全文。\n"
                    + "\n".join(lines) + tail)


# ══════════════════════════════════════════════════════════════════
# 插件 ④ Task（s11）
# ══════════════════════════════════════════════════════════════════

TASK_STATUSES = ("pending", "in_progress", "completed", "failed")


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str = "pending"
    depends_on: tuple[str, ...] = ()
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "title": self.title, "status": self.status}
        if self.depends_on:
            d["depends_on"] = list(self.depends_on)
        if self.note:
            d["note"] = self.note
        return d


class TaskStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self) -> list["Task"]:
        snapshot: list[dict[str, Any]] = []
        for ev in self.session.events():
            if ev.type == EV_TASK_WRITE:
                snapshot = ev.data["tasks"]
        return [Task(t["id"], t["title"], t.get("status", "pending"),
                     tuple(t.get("depends_on", [])), t.get("note", "")) for t in snapshot]

    def write(self, tasks: list["Task"]) -> None:
        self.session.append(EV_TASK_WRITE, {"tasks": [t.to_json() for t in tasks]})

    @staticmethod
    def validate(tasks: list["Task"]) -> str | None:
        ids = [t.id for t in tasks]
        if len(ids) != len(set(ids)):
            return "任务 id 重复"
        idset = set(ids)
        for t in tasks:
            for d in t.depends_on:
                if d not in idset:
                    return f"任务 {t.id} 依赖了不存在的 id：{d}"
                if d == t.id:
                    return f"任务 {t.id} 依赖了自己"
        remaining = {t.id: set(t.depends_on) for t in tasks}
        while remaining:
            ready = [i for i, deps in remaining.items() if not (deps & remaining.keys())]
            if not ready:
                return f"任务依赖成环：{', '.join(sorted(remaining))}"
            for i in ready:
                del remaining[i]
        by_id = {t.id: t for t in tasks}
        for t in tasks:
            if t.status in ("in_progress", "completed"):
                blocked = [d for d in t.depends_on if by_id[d].status != "completed"]
                if blocked:
                    return f"任务 {t.id} 标成了 {t.status}，但它依赖的 {', '.join(blocked)} 还没 completed"
        return None


class TaskPlugin:
    name = "tasks"

    def setup(self, ctx: PluginContext) -> None:
        store = TaskStore(ctx.session)
        ctx.provide("tasks", store)

        @ctx.tool("task_write",
                  "写入完整的任务清单（整表覆盖）。开始多步骤任务时先规划一次，之后每完成一项就重写一次。",
                  {"type": "object", "properties": {"tasks": {
                      "type": "array",
                      "description": "完整的任务列表。覆盖写，不是追加 —— 没列出来的任务会消失。",
                      "items": {"type": "object", "properties": {
                          "id": {"type": "string"}, "title": {"type": "string"},
                          "status": {"type": "string", "enum": list(TASK_STATUSES)},
                          "depends_on": {"type": "array", "items": {"type": "string"}},
                          "note": {"type": "string"}},
                          "required": ["id", "title", "status"]}}},
                   "required": ["tasks"]})
        def _task_write(tasks: list[dict[str, Any]]) -> str:
            try:
                parsed = [Task(str(t["id"]), str(t["title"]), str(t.get("status", "pending")),
                               tuple(t.get("depends_on", [])), str(t.get("note", "")))
                          for t in tasks]
            except (KeyError, TypeError) as e:
                return f"错误：任务格式不对（{e}）。每项至少要有 id / title / status。"
            bad = [t.id for t in parsed if t.status not in TASK_STATUSES]
            if bad:
                return f"错误：{', '.join(bad)} 的 status 不合法。只能是：{', '.join(TASK_STATUSES)}"
            problem = TaskStore.validate(parsed)
            if problem:
                return f"错误：{problem}。清单未更新，请修正后重新提交完整清单。"
            store.write(parsed)
            done = sum(1 for t in parsed if t.status == "completed")
            return f"任务清单已更新：{len(parsed)} 项，已完成 {done} 项。"

        @ctx.section("tasks", 15)
        def _tasks_section(rt: RuntimeContext) -> str | None:
            tasks = store.current()
            if not tasks:
                return None
            icon = {"pending": "○", "in_progress": "◐", "completed": "●", "failed": "✗"}
            lines = []
            for t in tasks:
                dep = f"  ← 依赖 {', '.join(t.depends_on)}" if t.depends_on else ""
                note = f"\n     备注：{t.note}" if t.note else ""
                lines.append(f"{icon.get(t.status, '?')} [{t.id}] {t.title}{dep}{note}")
            done = sum(1 for t in tasks if t.status == "completed")
            return (f"# 当前任务清单（{done}/{len(tasks)} 已完成）\n" + "\n".join(lines)
                    + "\n每完成一项就用 task_write 更新整个清单。这份清单是你的进度真相。")


# ══════════════════════════════════════════════════════════════════
# 插件 ⑤ Background Jobs（s12）
# ══════════════════════════════════════════════════════════════════


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "running"
    exit_code: int | None = None
    output: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    notified: bool = False

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.started_at


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def start_bash(self, command: str, cwd: Path, session: Session) -> Job:
        self._counter += 1
        job = Job(id=f"bash-{self._counter}", kind="bash", label=command[:120])
        with self._lock:
            self._jobs[job.id] = job
        session.append(EV_JOB_START, {"job": job.id, "kind": job.kind, "label": job.label})

        def runner() -> None:
            try:
                proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace")
                with self._lock:
                    self._procs[job.id] = proc
                out, _ = proc.communicate()
                with self._lock:
                    job.output = (out or "").strip()[:20000]
                    job.exit_code = proc.returncode
                    if job.status == "running":
                        job.status = "completed" if proc.returncode == 0 else "failed"
                    job.ended_at = time.time()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    job.status, job.output, job.ended_at = "failed", f"启动失败：{e}", time.time()

        threading.Thread(target=runner, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status == "running"]

    def stop(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return f"错误：没有 job {job_id}"
        if job.status != "running":
            return f"job {job_id} 已经是 {job.status} 状态"
        with self._lock:
            job.status = "killed"
            proc = self._procs.get(job_id)
        if proc:
            proc.kill()
        return f"已停止 job {job_id}"

    def take_finished_unnotified(self) -> list[Job]:
        out = []
        with self._lock:
            for j in self._jobs.values():
                if j.status != "running" and not j.notified:
                    j.notified = True
                    out.append(j)
        return out


_JOB_WORD = {"completed": "已成功完成", "failed": "已失败", "killed": "被停止"}


class JobPlugin:
    """后台任务：4 个工具 + 1 个 registry + 1 个 prompt 段 + 1 个 pre-step 监听器。

    这个插件最能说明"功能有边界"：s13 里它由 6 处散落的代码组成，
    卸载它要改 4 个地方；现在 `harness.unload("jobs")` 一行搞定。
    """

    name = "jobs"

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def setup(self, ctx: PluginContext) -> None:
        jobs = JobRegistry()
        ctx.provide("jobs", jobs)
        cwd = self.cwd

        @ctx.tool("bash_background",
                  "在后台启动一条 shell 命令，立即返回 job id，不等它跑完。适合耗时长的命令。",
                  {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]})
        def _bg(command: str) -> str:
            job = jobs.start_bash(command, cwd, ctx.session)
            return (f"已在后台启动 job {job.id}：{command}\n"
                    "它还在跑。你现在可以去做别的事，完成时我会通知你。")

        @ctx.tool("job_status", "查看后台任务的状态。不传 job_id 就列出全部。",
                  {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": []})
        def _status(job_id: str | None = None) -> str:
            if job_id:
                j = jobs.get(job_id)
                return (f"{j.id}  {j.status}  已运行 {j.elapsed:.1f}s  exit={j.exit_code}"
                        if j else f"错误：没有 job {job_id}")
            all_jobs = jobs.all()
            return ("\n".join(f"{j.id}  {j.status}  {j.elapsed:.1f}s  {j.label[:60]}" for j in all_jobs)
                    if all_jobs else "当前没有后台任务。")

        @ctx.tool("job_output", "读取后台任务的输出。任务还没结束时会告诉你还在跑。",
                  {"type": "object", "properties": {"job_id": {"type": "string"}},
                   "required": ["job_id"]})
        def _output(job_id: str) -> str:
            j = jobs.get(job_id)
            if j is None:
                return f"错误：没有 job {job_id}"
            if j.status == "running":
                return f"job {job_id} 还在运行（{j.elapsed:.1f}s）。先去做别的，完成时会通知你。"
            return f"job {job_id} {j.status}（exit={j.exit_code}）：\n{j.output or '(无输出)'}"

        @ctx.tool("job_stop", "停止一个还在运行的后台任务。",
                  {"type": "object", "properties": {"job_id": {"type": "string"}},
                   "required": ["job_id"]})
        def _stop(job_id: str) -> str:
            return jobs.stop(job_id)

        @ctx.section("jobs", 16)
        def _jobs_section(rt: RuntimeContext) -> str | None:
            running = jobs.running()
            if not running:
                return None
            lines = [f"◐ {j.id}  已运行 {j.elapsed:.0f}s  {j.label[:70]}" for j in running]
            return ("# 正在运行的后台任务\n" + "\n".join(lines)
                    + "\n它们完成时我会通知你。不要为了等它们而空转。")

        # 完成通知的注入点：s13 里它是 run_turn 里的一行 pump_jobs()，
        # 现在它是这个插件自己挂在 pre-step 上的监听器。
        # run_turn 因此完全不需要知道"后台任务"这个概念存在。
        @ctx.use(EVT_STEP_PRE, order=30)
        def _pump(pre: StepPreCtx, next_: Callable) -> None:
            for job in jobs.take_finished_unnotified():
                ctx.session.append(EV_JOB_END, {"job": job.id, "status": job.status,
                                                "exit_code": job.exit_code,
                                                "elapsed": round(job.elapsed, 2)})
                head = job.output[:1500]
                more = f"\n…（用 job_output({job.id}) 看完整内容）" if len(job.output) > 1500 else ""
                pre.items.append(InboxItem(
                    f"[后台任务 {job.id} {_JOB_WORD.get(job.status, job.status)}]\n"
                    f"命令：{job.label}\n耗时：{job.elapsed:.1f}s  exit={job.exit_code}\n"
                    f"输出：\n{head}{more}", source="job"))
            next_()


# ══════════════════════════════════════════════════════════════════
# 插件 ⑥ Subagent（s09）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SubagentPreset:
    name: str
    description: str
    tools: list[str]
    identity: str


# ── s16：preset 变成了**注册表** ──────────────────────────────────
#
# s09/s14 里 preset 是硬编码的 dict。这一章把它变成可注册的 ——
# 一个新角色 = 一次 register_preset()，不改 SubagentPlugin 一行。
#
# 角色定义在 Harness 侧（能力的信封），**派谁干什么**仍在模型侧。
SUBAGENT_PRESETS: dict[str, SubagentPreset] = {
    "explorer": SubagentPreset(
        "explorer", "只读探索。适合大范围搜索、通读代码、定位问题。",
        ["read", "glob", "grep"],
        "你是一个只读探索子 Agent。把任务查清楚后，用尽量短的话汇报结论，给出文件名和行号。"),
    "editor": SubagentPreset(
        "editor", "可读写。适合把已经明确的改动落地并自验证。",
        ["read", "glob", "grep", "edit", "write", "bash"],
        "你是一个执行子 Agent。任务已经明确，把它做完并自己验证，然后简短汇报。"),
    "reviewer": SubagentPreset(
        "reviewer", "只读审查。适合检查别人刚写的代码有没有问题。",
        ["read", "glob", "grep"],
        "你是一个审查子 Agent。只挑问题：bug、边界情况、规范违反。"
        "没发现问题就直说没有，不要为了显得有用而编问题。"),
}


def register_preset(preset: SubagentPreset) -> None:
    SUBAGENT_PRESETS[preset.name] = preset


class MemberAgent:
    """团队里一个**持续存活**的成员。

    s09 的子 Agent 是一次性的：spawn → 跑完 → 返回文本 → 丢弃。
    这里的成员有：
      · 自己的 Session / Inbox / RuntimeContext（和 s09 一样隔离）
      · 一个持续的消息队列 —— 别人可以随时发消息给它
      · 惰性运行：有消息时被 turn 处理，处理完回到 idle

    关键设计：它**不抢主模型的戏**。它只在自己的消息被认领时行动，
    行动的结果放在它的 outbox 里等主 Agent 来收。
    """

    def __init__(self, name: str, preset: SubagentPreset, parent: Session,
                 tools: ToolRegistry, bus: EventBus, tracer: Tracer,
                 provider_factory: Callable[[], Any]) -> None:
        self.name = name
        self.preset = preset
        self.inbox = Inbox()
        self.outbox: list[str] = []
        self.session = Session(path=(parent.path.parent / f"{parent.id}_agent_{name}.jsonl"
                                     if parent.path else None))
        self.session.append(EV_SESSION_START, {"parent": parent.id, "preset": preset.name, "agent": name})
        self.registry = tools.restricted(preset.tools)
        self.executor = ToolExecutor(self.registry, bus)
        self.prompts = SystemPromptRegistry()
        self.prompts.register(PromptSection("identity", 10, lambda rt: preset.identity))
        self.prompts.register(PromptSection(
            "tools", 40, lambda rt: f"# 工具\n可用工具：{', '.join(rt.tool_names)}"))
        self.prompts.register(PromptSection(
            "teammates", 45,
            lambda rt: "你在一个团队里。主 Agent 会通过消息给你任务；"
                       "做完后直接写一段结论文本，主 Agent 会收到。"))
        self.rt = RuntimeContext(cwd=Path("."))
        self.rt.cwd = Path.cwd() if not parent.path else parent.path.parent
        self.tracer = tracer
        self.provider_factory = provider_factory
        self.status = "idle"          # idle | working | done
        self.work_history: list[dict[str, Any]] = []
        self.delivered = 0          # 已交付给主 Agent 的产出条数（receive 的游标）

    def run_once(self) -> None:
        """处理当前 inbox 里的消息（如果有），处理完回到 idle。

        惰性设计：没有消息就不动。团队里谁在干活、什么时候干，
        由"谁收到了消息"决定 —— 这是协作机制的最小实现。
        """
        if not self.inbox:
            return
        self.status = "working"
        print(f"    \033[95m┌─ agent[{self.name}] 开始处理 {len(self.inbox._q)} 条消息\033[0m")
        outcome = run_turn(self.provider_factory(), None, self.executor, self.tracer,
                           prompt_registry=self.prompts, rt=self.rt,
                           session=self.session, inbox=self.inbox)
        self.status = "done" if outcome.steps else "idle"
        if outcome.text:
            self.outbox.append(outcome.text)
        self.work_history.append({"steps": outcome.steps, "reason": outcome.reason,
                                  "reply": outcome.text})
        print(f"    \033[95m└─ agent[{self.name}] 完成 steps={outcome.steps} "
              f"→ outbox {len(outcome.text)} 字符\033[0m")


class SubagentPlugin:
    """团队协作机制。四个动词：spawn / send / receive / list。

    注意它**没有**：
      · 没有"任务类型 → 派给谁"的路由（模型选 role）
      · 没有"先收集所有结果再继续"的工作流（模型自己 receive）
      · 没有自动转发、自动汇总

    s09 的一次性 spawn 保留为 spawn_agent 的默认形态；
    `persistent=True` 时创建**持续存活**的成员。
    """

    name = "subagent"

    def __init__(self, provider_factory: Callable[[], Any], tracer: Tracer, max_depth: int = 1) -> None:
        self.provider_factory = provider_factory
        self.tracer = tracer
        self.max_depth = max_depth
        self.depth = 0

    def setup(self, ctx) -> None:
        harness = ctx.harness
        members: dict[str, MemberAgent] = {}
        ctx.provide("agents", members)

        @ctx.tool("spawn_agent",
                  "派一个子 Agent 完成一项子任务。persistent=True 时会创建一名持续存活的"
                  "团队成员，之后可以用 send_message 继续对话。",
                  {"type": "object", "properties": {
                      "agent": {"type": "string", "description": "角色，见 system prompt 列表"},
                      "task": {"type": "string", "description": "任务描述，写清楚，它看不到你的对话历史。"},
                      "persistent": {"type": "boolean",
                                     "description": "True=创建可反复对话的成员；False=一次性执行完返回（默认）"}},
                   "required": ["agent", "task"]})
        def _spawn(agent: str, task: str, persistent: bool = False) -> str:
            preset = SUBAGENT_PRESETS.get(agent)
            if preset is None:
                return f"错误：没有 '{agent}' 这种角色。可选：{', '.join(SUBAGENT_PRESETS)}"

            if persistent:
                # ── 持续成员：放进团队，惰性启动 ─────────────────
                if agent in members:
                    return f"错误：角色 {agent} 已经有一名成员了（名字就是 {agent}）。"
                m = MemberAgent(agent, preset, ctx.session, harness.tools,
                                harness.bus, self.tracer, self.provider_factory)
                members[agent] = m
                m.inbox.put(task)
                ctx.session.append(EV_SUBAGENT_START, {
                    "agent": agent, "persistent": True, "task": task[:400],
                    "session": m.session.id, "tools": m.registry.names()})
                print(f"    \033[95m★ 团队成员 {agent} 加入（persistent）\033[0m")
                return (f"已创建团队成员 {agent}（持续存活）。它的第一项任务：{task[:200]}\n"
                        f"它完成时会产出结论，用 receive() 收取。之后可用 send_message({agent}, …) 继续对话。")

            # ── 一次性：s09 的老路径 ────────────────────────────
            parent = ctx.session
            child = Session(path=(parent.path.parent / f"{parent.id}_sub_{uuid.uuid4().hex[:6]}.jsonl"
                                  if parent.path else None))
            child.append(EV_SESSION_START, {"parent": parent.id, "preset": agent})
            child_registry = harness.tools.restricted(preset.tools)
            child_executor = ToolExecutor(child_registry, harness.bus)
            child_prompts = SystemPromptRegistry()
            child_prompts.register(PromptSection("identity", 10, lambda rt: preset.identity))
            child_prompts.register(PromptSection(
                "tools", 40, lambda rt: f"# 工具\n可用工具：{', '.join(rt.tool_names)}"))
            child_rt = RuntimeContext(cwd=harness.rt.cwd)
            child_inbox = Inbox()
            child_inbox.put(task)
            parent.append(EV_SUBAGENT_START, {"preset": agent, "persistent": False,
                                              "task": task[:400], "tools": child_registry.names()})
            print(f"    \033[95m┌─ subagent[{agent}] 启动（一次性）\033[0m")
            outcome = run_turn(self.provider_factory(), None, child_executor, self.tracer,
                               prompt_registry=child_prompts, rt=child_rt,
                               session=child, inbox=child_inbox)
            parent.append(EV_SUBAGENT_END, {"preset": agent, "persistent": False,
                                            "steps": outcome.steps,
                                            "result_chars": len(outcome.text)})
            print(f"    \033[95m└─ subagent[{agent}] 结束 steps={outcome.steps} "
                  f"→ 返回 {len(outcome.text)} 字符\033[0m")
            return outcome.text or "（子 Agent 没有返回结论）"

        @ctx.tool("send_message",
                  "给一名持续存活的团队成员发消息。它会在自己的上下文里处理并产出结论。",
                  {"type": "object", "properties": {
                      "agent": {"type": "string"},
                      "message": {"type": "string"}},
                   "required": ["agent", "message"]})
        def _send(agent: str, message: str) -> str:
            m = members.get(agent)
            if m is None:
                return f"错误：没有名为 {agent} 的团队成员。现有：{', '.join(members) or '（无）'}。"
            m.inbox.put(message)
            # 惰性运行：消息进 inbox 后立刻让成员处理。
            # 这样主 Agent 下一步 receive() 时结果已经在了。
            m.run_once()
            return f"已发送给 {agent}（{len(message)} 字符）。它已处理，用 receive() 收取结论。"

        @ctx.tool("receive",
                  "收取团队成员的产出。只读，不消耗 —— 结论会保留，直到你消化完。",
                  {"type": "object", "properties": {}, "required": []})
        def _receive() -> str:
            if not members:
                return "（还没有团队成员）"
            # receive = "处理完所有未处理的消息，然后给我产出"。
            # 这也是惰性运行的触发点：成员平时不动，被收到时才算账。
            # （send_message 也会触发一次 —— 两个触发点，谁先到谁跑。）
            for m in members.values():
                m.run_once()
            parts = []
            for name, m in members.items():
                # 只交付**新**产出：读过的不再重复。
                # 如果 receive 每次都把全部 outbox 再念一遍，
                # 模型每收一次就要重读一次旧结论，上下文被自己刷爆。
                fresh = m.outbox[m.delivered:]
                if fresh:
                    m.delivered = len(m.outbox)
                    parts.append(f"【{name} · {m.status}】\n" + "\n---\n".join(fresh))
            return "\n\n".join(parts) if parts else "（成员还没有新的产出）"

        @ctx.tool("list_agents",
                  "列出团队成员的名单和状态。",
                  {"type": "object", "properties": {}, "required": []})
        def _list() -> str:
            if not members:
                return "（团队还没有成员）"
            return "\n".join(f"{name}  {m.status}  inbox={len(m.inbox._q)} "
                              f"outbox={len(m.outbox)}  tools={','.join(m.registry.names())}"
                              for name, m in members.items())

        # 成员产出的被动提示：每一步 prompt 里都带一份团队快照
        @ctx.section("team", 44)
        def _team_section(rt: RuntimeContext) -> str | None:
            if not members:
                return None
            lines = [f"- {name}（{m.status}" +
                     (f"，有 {len(m.outbox)} 条产出待收" if m.outbox else "") + "）"
                     for name, m in members.items()]
            return ("# 团队\n以下是持续存活的团队成员。给它们派任务用 send_message，"
                    "收结论用 receive。\n" + "\n".join(lines))

        @ctx.section("subagents", 46)
        def _sub_section(rt: RuntimeContext) -> str:
            lines = [f"- {p.name}：{p.description}（工具：{', '.join(p.tools)}）"
                     for p in SUBAGENT_PRESETS.values()]
            return ("# 子 Agent 角色\n"
                    "可以用 spawn_agent 派一次性任务，或 persistent=True 创建持续成员。\n"
                    "子 Agent 有自己的上下文，看不到你的对话历史。\n"
                    "怎么组织协作（先派谁、派几个、何时收尾）由你自己决定。\n"
                    + "\n".join(lines))


# ══════════════════════════════════════════════════════════════════
# 插件 ⑦ Compaction（s10）
# ══════════════════════════════════════════════════════════════════

SUMMARIZE_SYSTEM = (
    "你在为一个正在工作的编程 Agent 压缩它的对话历史。\n"
    "把下面这段历史浓缩成一段简短的交接说明，必须保留：\n"
    "  1. 用户最初的目标\n  2. 已经查明的关键事实（文件名、行号、结论）\n"
    "  3. 已经做出的修改\n  4. 还没做完的事\n"
    "不要复述工具的原始输出。不要写客套话。")


def _project_range(session: Session, seqs: list[int]) -> list[dict[str, Any]]:
    want = set(seqs)
    out: list[dict[str, Any]] = []
    for ev in session.events():
        if ev.seq not in want:
            continue
        if ev.type == EV_USER_MESSAGE:
            out.append({"role": "user", "content": ev.data["content"]})
        elif ev.type == EV_ASSISTANT_MESSAGE:
            out.append({"role": "assistant", "content": ev.data.get("text", ""),
                        "tool_calls": ev.data.get("tool_calls") or []})
        elif ev.type == EV_TOOL_RESULT:
            out.append({"role": "tool", "content": ev.data["content"], "name": ev.data.get("name", "")})
    return out


def _render_for_summary(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            names = ", ".join(c["function"]["name"] for c in m["tool_calls"])
            lines.append(f"[assistant] {m.get('content', '')} (调用: {names})")
        elif m["role"] == "tool":
            lines.append(f"[tool:{m.get('name', '')}] {str(m['content'])[:600]}")
        else:
            lines.append(f"[{m['role']}] {m.get('content', '')}")
    return "\n".join(lines)


def find_safe_boundary(session: Session, keep_tokens: int) -> int | None:
    events = session.events()
    cuts = [e.seq for e in events if e.type == EV_STEP_END]
    if not cuts:
        return None
    _, already = collect_shadows(session)
    for cut in reversed(cuts):
        tail: list[dict[str, Any]] = []
        for ev in events:
            if ev.seq <= cut or ev.seq in already:
                continue
            if ev.type == EV_USER_MESSAGE:
                tail.append({"role": "user", "content": ev.data["content"]})
            elif ev.type == EV_ASSISTANT_MESSAGE:
                tail.append({"role": "assistant", "content": ev.data.get("text", ""),
                             "tool_calls": ev.data.get("tool_calls") or []})
            elif ev.type == EV_TOOL_RESULT:
                tail.append({"role": "tool", "content": ev.data["content"]})
        if estimate_tokens(tail) >= keep_tokens:
            return cut
    return cuts[0]


class CompactionPlugin:
    """上下文压缩。它挂在 agent/pre-step 上，自己判断压力、自己动手。

    s13 里这段逻辑写在 run_turn 里（`if estimate_tokens(...) > ...: compact(...)`）。
    现在 run_turn 完全不知道有"压缩"这回事。
    """

    name = "compaction"

    def __init__(self, summarizer: Any, tracer: Tracer) -> None:
        self.summarizer = summarizer
        self.tracer = tracer

    def setup(self, ctx: PluginContext) -> None:
        @ctx.use(EVT_STEP_PRE, order=50)
        def _maybe_compact(pre: StepPreCtx, next_: Callable) -> None:
            session = ctx.session
            if estimate_tokens(derive_messages(session)) > CONTEXT_LIMIT_TOKENS * COMPACT_TRIGGER:
                self.compact(session)
            next_()

        ctx.provide("compaction", self)

    def compact(self, session: Session) -> bool:
        boundary = find_safe_boundary(session, int(CONTEXT_LIMIT_TOKENS * KEEP_RECENT_RATIO))
        if boundary is None:
            return False
        prior_anchors, already = collect_shadows(session)
        prior_summary_seqs = [e.seq for e in session.events()
                              if e.type == EV_COMPACTION_SUMMARY
                              and min(e.data["shadowed_seqs"]) in prior_anchors]
        fresh = [e.seq for e in session.events()
                 if e.seq <= boundary and e.type in SURFACE_EVENTS and e.seq not in already]
        if not fresh or estimate_tokens(_project_range(session, fresh)) < CONTEXT_LIMIT_TOKENS * 0.15:
            return False
        to_shadow = sorted(set(fresh) | already)

        session.append(EV_COMPACTION_START, {"boundary": boundary})
        before = estimate_tokens(derive_messages(session))
        carry = "\n\n".join(prior_anchors.values())
        body = ((f"[上一次压缩的摘要]\n{carry}\n\n" if carry else "")
                + _render_for_summary(_project_range(session, fresh)))
        try:
            summary_text = self.summarizer.chat(
                [{"role": "user", "content": "以下是需要压缩的历史：\n\n" + body}],
                system=SUMMARIZE_SYSTEM).text.strip()
        except LLMError as e:
            session.append(EV_COMPACTION_END, {"boundary": boundary, "error": str(e)})
            return False
        if not summary_text:
            session.append(EV_COMPACTION_END, {"boundary": boundary, "error": "空摘要"})
            return False

        session.append(EV_COMPACTION_SUMMARY, {
            "shadowed_seqs": to_shadow, "supersedes": prior_summary_seqs,
            "summary": f"[以下是之前 {len(to_shadow)} 条消息的压缩摘要]\n\n{summary_text}",
            "tokens_before": before})
        session.append(EV_COMPACTION_END, {"boundary": boundary})
        after = estimate_tokens(derive_messages(session))
        print(f"  \033[95m⟲ 上下文压缩：{len(to_shadow)} 条消息 → 1 条摘要  "
              f"{before} → {after} tokens\033[0m")
        return True


# ══════════════════════════════════════════════════════════════════
# s17 新增：Goal
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Goal:
    """一个跨轮持续的目标。

    和 s11 的 Task 是不同维度：
        Goal   用户要的**结果**    —— "修好 divide 的除零问题"，有预算、有生命周期
        Task   模型拆的**步骤**    —— "给 divide 加除零保护"，跨 turn 的进度清单

    一个 Goal 通常产生多个 Task；评估 Goal 时看的不是 Task 清单，
    而是"用户要的结果达成了没有"。
    """

    statement: str
    status: str = "active"            # active | paused | blocked | complete
    round: int = 0                    # 已经自动继续了几轮
    max_rounds: int = 5               # 预算：最多自动继续几轮
    reason: str = ""                  # blocked/complete 的原因


class GoalStore:
    """目标 —— 又一个从事件日志派生的视图（和 TaskStore 同一个模式）。

    用事件而不是一个内存变量，理由和 s05 完全一样：
    目标是**事实**，事实必须能从日志重建。

    进程重启 → Session.load → GoalStore.current() 回来。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self) -> Goal | None:
        latest: dict[str, Any] | None = None
        for ev in self.session.events():
            if ev.type in (EV_GOAL_START, EV_GOAL_BLOCKED, EV_GOAL_COMPLETE):
                latest = ev.data
        if latest is None:
            return None
        return Goal(statement=latest.get("statement", ""), status=latest.get("status", "active"),
                    round=latest.get("round", 0), max_rounds=latest.get("max_rounds", 5),
                    reason=latest.get("reason", ""))

    def set(self, goal: Goal) -> None:
        self.session.append(EV_GOAL_START, {
            "statement": goal.statement, "status": goal.status,
            "round": goal.round, "max_rounds": goal.max_rounds, "reason": goal.reason})

    def update(self, event: str, status: str, round_: int, reason: str, statement: str,
               max_rounds: int) -> None:
        self.session.append(event, {"statement": statement, "status": status,
                                    "round": round_, "max_rounds": max_rounds,
                                    "reason": reason})


EVALUATE_SYSTEM = (
    "你是一个任务评估器。下面是一个编程 Agent 最近的工作记录和它负责的目标。\n"
    "只回答一个词加一行理由：\n"
    "  done     目标已经达成，可以收工\n"
    "  blocked  目标被某个问题卡住，继续自动重试没有意义\n"
    "  continue 目标还没达成，但 Agent 应该继续\n"
    "回答格式：\n"
    "verdict: <done|blocked|continue>\n"
    "reason: <一行中文>")


class GoalPlugin:
    """把"目标"这个持久状态接进 harness。

    它的全部逻辑是一个**观察者**（挂在 turn 结束时）：

        turn 结束 → 目标还是 active？还有预算吗？
          → 问评估器：done / blocked / continue？
            → done     → 记 goal/complete，不再打扰模型
            → blocked  → 记 goal/blocked（带原因），不再烧钱
            → continue → 注入一条"[目标未完成] 继续"，开新一轮

    注意 Harness 在这里**不判断任务内容**。
    它判断的是：有没有预算、评估器说了什么。
    "下一步做什么"依然在注入的那条提示之后，由模型自己决定。
    """

    name = "goal"

    def __init__(self, evaluator: Any, max_rounds: int = 5) -> None:
        self.evaluator = evaluator
        self.max_rounds = max_rounds

    def setup(self, ctx) -> None:
        store = GoalStore(ctx.session)
        ctx.provide("goal", store)

        @ctx.section("goal", 14)
        def _goal_section(rt: RuntimeContext) -> str | None:
            g = store.current()
            if g is None or g.status != "active":
                return None
            return (f"# 当前目标（第 {g.round + 1}/{g.max_rounds} 轮）\n"
                    f"{g.statement}\n"
                    "每个轮次结束时 Harness 会评估这个目标。你的工作是把它推进到完成。")

        @ctx.on(EVT_TURN_END)
        def _after_turn(turn: int, reason: str, steps: int) -> None:
            goal = store.current()
            if goal is None or goal.status != "active":
                return
            if reason == "no-input" or reason == "rejected":
                return
            # 预算用尽：不再自动继续，记下来，等人来
            if goal.round >= goal.max_rounds:
                store.update(EV_GOAL_BLOCKED, "blocked", goal.round,
                             "预算用尽（max_rounds）", goal.statement, goal.max_rounds)
                print(f"  \033[35m◼ goal blocked: 预算用尽（{goal.max_rounds} 轮）\033[0m")
                return

            # 问评估器 —— 这是额外的模型调用，所以只在有 active goal 时花
            try:
                msgs = derive_messages(ctx.session)[-24:]      # 只看最近的工作
                text = "\n".join(str(m.get("content", ""))[:800] for m in msgs)
                reply = self.evaluator.chat(
                    [{"role": "user", "content":
                      f"目标：{goal.statement}\n\n最近的工作记录：\n{text[-6000:]}"}],
                    system=EVALUATE_SYSTEM)
                verdict, reason_line = _parse_verdict(reply.text)
            except LLMError as e:
                # 评估器挂了：保守地当作 blocked 处理，不假装成功也不无限烧钱
                ctx.session.append(EV_GOAL_EVALUATE, {"turn": turn, "verdict": "error",
                                                      "error": str(e)})
                store.update(EV_GOAL_BLOCKED, "blocked", goal.round,
                             f"评估器不可用：{e}", goal.statement, goal.max_rounds)
                print(f"  \033[35m◼ goal blocked: 评估器不可用\033[0m")
                return

            ctx.session.append(EV_GOAL_EVALUATE, {"turn": turn, "verdict": verdict,
                                                  "reason": reason_line, "round": goal.round})

            if verdict == "done":
                store.update(EV_GOAL_COMPLETE, "complete", goal.round,
                             reason_line, goal.statement, goal.max_rounds)
                print(f"  \033[32m● goal complete：{reason_line}\033[0m")

            elif verdict == "blocked":
                store.update(EV_GOAL_BLOCKED, "blocked", goal.round,
                             reason_line, goal.statement, goal.max_rounds)
                print(f"  \033[35m◼ goal blocked：{reason_line}\033[0m")

            else:   # continue
                new_round = goal.round + 1
                store.update(EV_GOAL_START, "active", new_round,
                             reason_line, goal.statement, goal.max_rounds)
                ctx.inbox.put(
                    f"[目标还未完成 · 第 {new_round} 轮 · 评估意见：{reason_line}]\n"
                    f"继续推进目标：{goal.statement}",
                    source="goal")
                print(f"  \033[35m↻ goal continue（第 {new_round}/{goal.max_rounds} 轮）："
                      f"{reason_line}\033[0m")


def _parse_verdict(text: str) -> tuple[str, str]:
    """解析评估器的输出。格式简单，但容错要够 ——
    评估器输出错格式时，宁可当作 continue（让 Agent 再试一轮）
    也不要误判 done 提前收工。"""
    v, r = "continue", ""
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("verdict:"):
            word = low.split("verdict:", 1)[1].strip().split()[0]
            v = word if word in ("done", "blocked", "continue") else "continue"
        elif low.startswith("reason:"):
            r = line.split("reason:", 1)[1].strip()
    if not r:
        r = "（评估器没有给出理由）"
    return v, r


# ══════════════════════════════════════════════════════════════════
# Profile：一份插件清单 = 一个产品形态
# ══════════════════════════════════════════════════════════════════


def build_harness(profile: str, cwd: Path, session: Session, tracer: Tracer,
                  provider_factory: Callable[[], Any], summarizer: Any,
                  approver: Approver, skills_root: Path,
                  fs: FileSystem | None = None, shell: Shell | None = None) -> Harness:
    """fs/shell 不传就用本地实现 —— 传了就换世界。

    这是 seam 的入口：一个参数决定 Agent 活在哪。
    """
    """装配。

    整个程序里**只有这一个函数**知道系统由哪些功能组成。
    s13 里这些代码在 demo() 和 main() 里各抄了一遍，改一处就要改两处。

    profile 就是"一份插件清单"。加一个产品形态 = 加一个 elif 分支，
    不需要改任何插件、不需要改 run_turn、不需要改 ToolExecutor。
    """
    h = Harness(session, cwd)

    # 每个 profile 都要的地基。
    # 注意顺序：capabilities 必须最先装 —— CoreToolsPlugin 会 require("fs")。
    h.use(CapabilityPlugin(fs or LocalFileSystem(cwd), shell or LocalShell()))
    h.use(IdentityPlugin())
    h.use(CoreToolsPlugin())
    h.use(SessionLogPlugin())
    h.use(ValidationPlugin())
    h.use(TracePlugin(tracer))
    h.use(RedactPlugin())
    h.use(TruncatePlugin())

    if profile == "readonly":
        # 只读版：权限策略换一个，写类插件干脆不装。
        # 注意"不装"和"装了但禁用"的区别 —— 不装意味着模型的 prompt 里
        # 根本没有那些工具，它不会想着去试。
        h.use(PermissionPlugin(PermissionPolicy(read_only=True), approver))
        h.use(SkillPlugin(skills_root))
        h.use(SubagentPlugin(provider_factory, tracer))
        return h

    if profile == "minimal":
        # 最小版：只有工具和权限。没有技能、任务、后台、子 Agent、压缩。
        h.use(PermissionPlugin(PermissionPolicy(yolo=True), approver))
        return h

    # full：全部功能
    h.use(PermissionPlugin(PermissionPolicy(yolo=(profile == "yolo")), approver))
    h.use(TimingPlugin())
    h.use(SkillPlugin(skills_root))
    h.use(TaskPlugin())
    h.use(JobPlugin(cwd))
    h.use(SubagentPlugin(provider_factory, tracer))
    h.use(CompactionPlugin(summarizer, tracer))
    h.use(GoalPlugin(summarizer, max_rounds=4))
    return h


def print_harness(h: Harness) -> None:
    print(f"\n\033[1m已加载 {len(h.loaded())} 个插件\033[0m")
    print(f"  {' · '.join(h.loaded())}")
    print(f"\n\033[1m它们一共贡献了\033[0m")
    print(f"  工具 {len(h.tools.names()):>2} 个：{', '.join(h.tools.names())}")
    print(f"  prompt 段 {len(h.prompts.names())} 个：{', '.join(h.prompts.names())}")
    print(f"  service {len(h.services)} 个：" +
          (", ".join(f"{k}(by {h.service_owner[k]})" for k in h.services) or "（无）"))
    n_listeners = sum(len(items) for _, _, items in h.bus.describe())
    print(f"  监听器 {n_listeners} 个")


def print_bus(bus: EventBus) -> None:
    print("\n\033[1m总线\033[0m \033[90m（order 小的在外层）\033[0m")
    for event, kind, items in bus.describe():
        tag = "\033[35mwaterfall\033[0m" if kind == "waterfall" else "\033[36memit     \033[0m"
        print(f"  {tag} \033[33m{event:<18}\033[0m {' → '.join(items)}")


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def build_demo_workspace() -> Path:
    d = Path(tempfile.mkdtemp(prefix="s14_demo_"))
    (d / "app.py").write_text('VERSION = "0.1.0"\n\ndef main():\n    print(VERSION)\n', encoding="utf-8")
    (d / "config.env").write_text("API_KEY=sk-abcdef0123456789\nDEBUG=1\n", encoding="utf-8")
    (d / "AGENTS.md").write_text("- 改完代码要跑一次 python3 app.py 验证\n", encoding="utf-8")
    return d


def load_project_notes(cwd: Path) -> str | None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = cwd / name
        if f.exists():
            return f.read_text(encoding="utf-8")[:4000]
    return None


def build_buggy_project() -> Path:
    """一个有真实 bug 的小项目：验收场景的靶子。"""
    d = Path(tempfile.mkdtemp(prefix="s18_demo_"))
    (d / "calc").mkdir()
    (d / "tests").mkdir()
    (d / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (d / "calc" / "maths.py").write_text(
        "\n".join([
            "def add(a, b):",
            "    return a + b",
            "",
            "def divide(a, b):",
            "    return a / b    # 没有除零保护",
            "",
            "def multiply(a, b):",
            "    return a * b",
        ]), encoding="utf-8")
    # 用纯标准库的测试脚本（本机不一定装了 pytest，demo 必须离线可跑）
    (d / "tests" / "test_maths.py").write_text(
        "\n".join([
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))",
            "",
            "from calc.maths import add, divide, multiply",
            "",
            "def check(name, actual, expected):",
            "    ok = actual == expected",
            "    print(f'{name}: {\"PASS\" if ok else \"FAIL\"}')",
            "    assert ok, f'{name} 期望 {expected}，实际 {actual}'",
            "",
            "check('test_add', add(2, 3), 5)",
            "check('test_divide', divide(10, 2), 5)",
            "check('test_divide_by_zero', divide(1, 0), None)   # 期望除零返回 None",
            "check('test_multiply', multiply(3, 4), 12)",
            "print('全部 4 个检查通过')",
        ]), encoding="utf-8")
    (d / "README.md").write_text("# calc\n\n一个演示用的小计算库。\n", encoding="utf-8")
    return d


def demo(debug: bool) -> None:
    tracer = Tracer(enabled=debug)
    skills_root = Path(__file__).resolve().parent / "skills"
    cwd = build_buggy_project()

    sess = Session(path=cwd / "session.jsonl")
    sess.append(EV_SESSION_START, {"cwd": str(cwd)})

    h = build_harness("full", cwd, sess, tracer,
                      lambda: get_provider(demo_script=[scripted("（未使用）")]),
                      get_provider(demo_script=[scripted("verdict: done\nreason: 测试已全部通过。")]),
                      lambda *a: True, skills_root)
    h.rt.project_notes = load_project_notes(cwd)
    executor = ToolExecutor(h.tools, h.bus)

    print("\033[1m【1】这台机器由什么组成\033[0m")
    print_harness(h)

    print("\n\033[1m【2】验收：模型自主修复失败的测试\033[0m")
    print("\033[90m  任务：帮我检查这个项目为什么测试失败，并修复它。\033[0m")
    print("\033[90m  Harness 不知道这是 debugging task —— 它只提供了工具和状态。\033[0m")
    h.inbox.put("帮我检查这个项目为什么测试失败，并修复它。")

    # 模型（由脚本假扮）的自主路径 —— 每一步都是它自己决定调什么：
    # 先看项目结构 → 跑测试 → 读报错 → 定位 → 修复 → 重跑验证
    script = [
        scripted(calls=[("glob", {"pattern": "**/*.py"})]),
        scripted(calls=[("bash", {"command": "python3 tests/test_maths.py"})]),
        scripted(calls=[("read", {"path": "calc/maths.py"})]),
        scripted(calls=[("read", {"path": "tests/test_maths.py"})]),
        scripted(calls=[("edit", {
            "path": "calc/maths.py",
            "old_text": "def divide(a, b):\n    return a / b    # 没有除零保护",
            "new_text": "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b"})]),
        scripted(calls=[("bash", {"command": "python3 tests/test_maths.py"})]),
        scripted("测试失败的根因：tests/test_maths.py:9 期望 divide(1, 0) 返回 None，"
                 "但原实现直接 a / b 抛 ZeroDivisionError。已给 divide 加除零保护，"
                 "4 个测试全部通过。"),
    ]
    out = run_turn(get_provider(demo_script=script), h, executor, tracer)
    print(f"\033[32m模型 >\033[0m {out.text}")

    print("\n\033[1m【3】修复后的代码与验证\033[0m")
    for line in (cwd / "calc" / "maths.py").read_text(encoding="utf-8").splitlines():
        print(f"  \033[90m{line}\033[0m")

    print("\n\033[1m【4】这台机器上跑过的机制（全部由插件/事件提供）\033[0m")
    n_listeners = sum(len(i) for _, _, i in h.bus.describe())
    steps = sum(1 for e in sess.events() if e.type == EV_STEP_START)
    rows = [
        ("s01 循环", "run_turn 的 while，一次没改"),
        ("s03 注册表", f"{len(h.tools.names())} 个工具，全部来自插件注册"),
        ("s04 权限", "PermissionPlugin（demo 用 yolo）"),
        ("s05 日志", f"{len(sess)} 条事件落盘，可 replay"),
        ("s06 turn/step", f"{steps} 个 step 有完整边界记录"),
        ("s07 prompt", f"{len(h.prompts.names())} 个 section 每步组装"),
        ("s08 技能", "目录常驻，正文按需（这次没加载）"),
        ("s09/s16 团队", "可用但模型这次决定不用"),
        ("s10 压缩", "CompactionPlugin 挂在 pre-step（这次没触发）"),
        ("s11 任务", "模型这次决定不拆 task_write"),
        ("s12 后台", "可用但这次没用到"),
        ("s13 事件", f"{n_listeners} 个监听器贯穿每次工具调用"),
        ("s14 插件", f"{len(h.loaded())} 个插件组成这台机器"),
        ("s15 seam", "LocalFileSystem + LocalShell"),
        ("s17 goal", "评估器在 turn 结束时判定 done"),
    ]
    for tag, desc in rows:
        print(f"  \033[90m{tag:<10}\033[0m {desc}")

    print("\n\033[1m【5】「Harness 不替模型思考」的最后一次检查\033[0m")
    # 搜的是**决策分支的代码形态**，而不是裸词 ——
    # 裸词会命中这段检查代码自身。决策分支长这样：
    #   if task_type == "research": …   ← 禁止
    import re as _re
    full = Path(__file__).read_text(encoding="utf-8")
    # 只搜 harness 本体（demo 函数之前的代码）：
    # demo 里恰好有"禁止写法示例"的注释，搜它会自匹配。
    src = full[: full.index("def demo(debug: bool) -> None:")]
    # 检查词用拼接构造，避免命中检查代码自身。
    t = "task" + "_type"
    patterns = [r"if\s+" + t, r"elif\s+" + t, "call_research" + "_agent",
                "call_coding" + "_agent", "intent" + "_classif", "router" + "." + "route"]
    hits = [pat for pat in patterns if _re.search(pat, src)]
    print(f"  harness 本体搜索 if task_type / call_xxx_agent / router.route："
          f"\033[32m{hits or '零命中'}\033[0m")
    print("\033[90m  模型调用的 6 个工具（glob→bash→read→read→edit→bash）没有一个是")
    print("  Harness 提前写好的步骤 —— 它们是模型在每步观察后自己产生的。\033[0m")

    print("\n\033[90m" + "─" * 68)
    print("18 章走完了。最后留一句话：")
    print()
    print("  Agent 的智能主要来自模型。")
    print("  Harness 的价值不是替模型写死思考流程，")
    print("  而是为模型构建一个拥有工具、环境、上下文、状态、")
    print("  权限和反馈的可操作世界。\033[0m")


def _goal_text(h: Harness) -> str:
    for name, sec in h.prompts._sections.items():
        if name == "goal":
            return sec.render(h.rt) or ""
    return ""



def main() -> None:
    debug = "--debug" in sys.argv
    if "--demo" in sys.argv:
        demo(debug)
        return

    profile = "full"
    if "--profile" in sys.argv:
        profile = sys.argv[sys.argv.index("--profile") + 1]

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return

    cwd = Path.cwd()
    tracer = Tracer(enabled=debug)
    log_path = Path(f"session_{uuid.uuid4().hex[:8]}.jsonl")
    session = Session(path=log_path)
    session.append(EV_SESSION_START, {"cwd": str(cwd)})

    h = build_harness(profile, cwd, session, tracer,
                      provider_factory=lambda: provider, summarizer=provider,
                      approver=cli_approver,
                      skills_root=Path(__file__).resolve().parent / "skills")
    h.rt.project_notes = load_project_notes(cwd)
    executor = ToolExecutor(h.tools, h.bus)

    print(f"\033[1ms17 — Goal Loop\033[0m  \033[90mprofile={profile}\033[0m")
    print(f"\n\033[90m/goal <一句话> 设定目标；/goal 看状态；日志 {log_path}；q 退出\033[0m\n")

    while True:
        try:
            q = input("\033[36m你 > \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("q", "quit", "exit"):
            break
        if q.startswith("/goal"):
            store = h.get("goal")
            rest = q[5:].strip()
            if rest:
                store.set(Goal(statement=rest, max_rounds=4))
                print(f"\033[36m  目标已设定（预算 4 轮）：{rest}\033[0m")
            else:
                g = store.current()
                if g is None:
                    print("\033[90m  （还没有目标）\033[0m")
                else:
                    print(f"  {g.status}  round={g.round}/{g.max_rounds}  {g.statement}"
                          + (f"  \033[90m// {g.reason}\033[0m" if g.reason else ""))
            continue
        if q == "/agents":
            members = h.services.get("agents", {})
            print("\n".join(f"{n}  {m.status}  outbox={len(m.outbox)}"
                             for n, m in members.items()) or "（团队还没有成员）")
            continue
        if q == "/plugins":
            print_harness(h)
            continue
        if q == "/caps":
            for key in ("fs", "shell"):
                print(f"  {key}: {type(h.get(key)).__name__}")
            continue
        if q:
            h.inbox.put(q)
        try:
            out = run_turn(provider, h, executor, tracer)
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            break
        if out.reason == "no-input":
            print("\033[90m  （没有新输入）\033[0m")
            continue
        print(f"\033[32m模型 >\033[0m {out.text}")
        print(f"\033[90m[turn {out.turn} · {out.steps} steps · {out.reason}]\033[0m\n")






if __name__ == "__main__":
    main()
