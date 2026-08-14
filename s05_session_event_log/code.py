#!/usr/bin/env python3
"""s05 — Session Event Log

    事件日志（append-only，唯一真相）
         │
         │  derive_messages()
         ▼
    LLM messages（投影，随时可重算，不存储）

这一章是整门课的枢纽，它推翻了前四章的一个隐含假设：

    ❌ messages 是真相
    ✅ messages 是**事件日志的一个投影**

一句话总结这一章：

    Model-visible means logged.
    凡是能进入模型请求的东西，都必须能从日志重建出来。

运行：
    python s05_session_event_log/code.py --demo      # 完整演示（含中断/恢复）
    python s05_session_event_log/code.py --replay <session.jsonl>
    python s05_session_event_log/code.py
"""

import glob as globlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

MAX_STEPS = 20


# ══════════════════════════════════════════════════════════════════
# s05 新增：Session Event Log
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SessionEvent:
    """日志里的一条事实。

    三个性质，缺一不可：

      · 只追加     写下去就不改。"改历史"这件事一旦允许，
                   回放、fork、审计就全部失去意义。
      · 有序号     seq 单调递增且连续。它是事件的身份，
                   也是"上下文压缩"这类操作能精确指定范围的前提。
      · 可序列化   data 必须是纯 JSON。存不下来的事件等于没记。
    """

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
        return SessionEvent(seq=d["seq"], type=d["type"], data=d["data"], time=d.get("time", 0.0))


# ── 事件类型表 ────────────────────────────────────────────────────
#
# 分成两类，这个划分是本章最需要记住的东西：
#
#   SURFACE（会变成模型消息）      —— 模型看得见
#   log-only（只进日志，不进上下文）—— 模型看不见，但我们要
#
# 为什么需要 log-only？因为有大量事实**必须记录但不该给模型看**：
#   · 用户在第 3 步点了"拒绝"        → 记；但模型只需要看到那条拒绝结果
#   · 这次请求烧了 4200 token        → 记；灌给模型纯属浪费
#   · 工具在执行前就已经被登记了      → 记；崩溃时才能知道执行到哪了
#
# s04 之前这些东西无处安放：塞进 messages 会污染上下文，不塞就丢了。
# 事件日志给了它们一个家。

EV_SESSION_START = "session/start"     # log-only
EV_USER_MESSAGE = "user/message"       # SURFACE
EV_ASSISTANT_MESSAGE = "assistant/message"  # SURFACE
EV_TOOL_CALL = "tool/call"             # log-only（见下方说明）
EV_TOOL_RESULT = "tool/result"         # SURFACE
EV_PERMISSION = "permission/decision"  # log-only
EV_USAGE = "request/usage"             # log-only

SURFACE_EVENTS = {EV_USER_MESSAGE, EV_ASSISTANT_MESSAGE, EV_TOOL_RESULT}

# 为什么 tool/call 是 log-only？
#
# 模型请求了哪些工具，这个信息已经在 assistant/message 里了
# （assistant 消息本身就带 tool_calls），再投影一次就重复了。
#
# 但它仍然必须**单独记一条**，而且是在**执行之前**记。
# 理由：一条 tool/call 后面没有配对的 tool/result，
# 就是"执行到一半崩了"的铁证。如果只在执行完之后记一条，
# 崩溃现场会看起来像"这次调用从未发生" —— 那是最危险的一种日志。


class Session:
    """一次会话 = 一条 append-only 的事件流。

    注意它**没有** messages 字段。
    模型上下文不是存储出来的，是 derive_messages() 算出来的。

    这个设计换来四件前四章做不到的事：

      1. 恢复   进程挂了，读日志重建，一条不丢
      2. 回放   任意时刻的上下文都能重算（"第 3 步时模型看见了什么？"）
      3. 分叉   从第 N 条事件切一刀，得到一个新会话
      4. 审计   决策、token、权限判定各归其位，且和消息在同一条时间线上

    s04 的 executor.audit 是一个独立的 list，它和 messages 对不上号 ——
    审计说"第 4 次调用被拒了"，但那是 messages 里第几条？没人知道。
    现在它们在同一条流里，seq 就是答案。
    """

    def __init__(self, session_id: str | None = None, path: Path | None = None) -> None:
        self.id = session_id or f"ses_{uuid.uuid4().hex[:10]}"
        self.path = path
        self._events: list[SessionEvent] = []
        self._seq = 0

    # ── 写 ────────────────────────────────────────────────────────
    def append(self, type_: str, data: dict[str, Any]) -> SessionEvent:
        # 序列化检查放在写入时，而不是持久化时。
        # 一个存不下来的事件必须**当场**炸，否则你会在崩溃恢复时
        # 才发现日志早就残缺了 —— 那时已经晚了。
        json.dumps(data, ensure_ascii=False)

        self._seq += 1
        ev = SessionEvent(seq=self._seq, type=type_, data=data)
        self._events.append(ev)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(ev.to_json() + "\n")
        return ev

    # ── 读 ────────────────────────────────────────────────────────
    def events(self, upto: int | None = None) -> list[SessionEvent]:
        return [e for e in self._events if upto is None or e.seq <= upto]

    def __len__(self) -> int:
        return len(self._events)

    @classmethod
    def load(cls, path: Path) -> "Session":
        """从磁盘重建。注意重建的是**事件**，不是 messages。

        messages 会在第一次 derive_messages() 时重新算出来 ——
        这正是"投影"的含义：它不需要被保存，因为它随时可以重算。
        """
        s = cls(session_id=path.stem, path=path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ev = SessionEvent.from_json(line)
                s._events.append(ev)
                s._seq = max(s._seq, ev.seq)
        return s


def derive_messages(session: Session, upto: int | None = None) -> list[dict[str, Any]]:
    """事件日志 ──▶ LLM messages。

    这个函数是本章的核心。它是一个**纯投影**：
    同样的事件流永远得到同样的 messages，没有任何隐藏状态。

    只有 SURFACE_EVENTS 参与投影。其余事件（权限决定、token 用量、
    tool/call 登记）留在日志里，不进上下文。

        Model-visible means logged.
        但反过来不成立：logged 的东西不一定 model-visible。

    这一点是很多人第一次读工业 Harness 时的困惑来源，
    这里把它写成了两个显式的集合，就不会混。
    """
    messages: list[dict[str, Any]] = []
    for ev in session.events(upto):
        if ev.type not in SURFACE_EVENTS:
            continue
        if ev.type == EV_USER_MESSAGE:
            messages.append({"role": "user", "content": ev.data["content"]})
        elif ev.type == EV_ASSISTANT_MESSAGE:
            # 原样还原，包括 tool_calls —— 否则下一轮模型对不上自己的意图
            msg: dict[str, Any] = {"role": "assistant", "content": ev.data.get("text", "")}
            if ev.data.get("tool_calls"):
                msg["tool_calls"] = ev.data["tool_calls"]
            messages.append(msg)
        elif ev.type == EV_TOOL_RESULT:
            messages.append({
                "role": "tool",
                "tool_call_id": ev.data["call_id"],
                "content": ev.data["content"],
            })
    return messages


# ══════════════════════════════════════════════════════════════════
# 沿用 s04（未改动）：Tool / ToolResult / ToolRegistry / 权限
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

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, parameters: dict[str, Any]) -> Callable:
        def deco(fn):
            self.register(Tool(name, description, parameters, fn))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]


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
    r"python3?\s+--version|uname|env|df|du)\b"
)


class PermissionPolicy:
    def __init__(self, yolo: bool = False) -> None:
        self.yolo = yolo

    def check(self, name: str, args: dict[str, Any]) -> Verdict:
        if self.yolo:
            return Verdict(Decision.ALLOW, "yolo 模式")
        if name in ("read", "glob", "grep"):
            return Verdict(Decision.ALLOW, "只读操作")
        if name in ("write", "edit"):
            return Verdict(Decision.ASK, f"将修改文件 {args.get('path', '?')}")
        if name == "bash":
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
# s05 改写：ToolExecutor 不再自己攒 audit，改为往 Session 写事件
# ══════════════════════════════════════════════════════════════════


@dataclass
class ToolCallCtx:
    call_id: str
    name: str
    arguments: dict[str, Any]
    verdict: Verdict | None = None


class ToolExecutor:
    """管线三段没变，变的是**它把事实写到哪里**。

    s04：self.audit.append({...})     ← 一个孤立的 list
    s05：session.append("permission/decision", {...})   ← 和消息同一条流

    这个改动看起来很小，实际意义很大：审计记录和模型上下文
    从此有了**共同的时间线**。"第 7 号事件是权限拒绝，
    第 8 号事件是模型看到的那条拒绝结果" —— 现在这句话是可查证的。
    """

    def __init__(self, registry: ToolRegistry, policy: PermissionPolicy, approver: Approver) -> None:
        self.registry = registry
        self.policy = policy
        self.approver = approver

    def pre_execute(self, ctx: ToolCallCtx, session: Session) -> ToolResult | None:
        tool = self.registry.get(ctx.name)
        if tool is None:
            return ToolResult(f"错误：没有名为 '{ctx.name}' 的工具。可用工具：{', '.join(self.registry.names())}",
                              is_error=True)
        missing = [k for k in tool.required if k not in ctx.arguments]
        if missing:
            return ToolResult(f"错误：{ctx.name} 缺少必填参数：{', '.join(missing)}", is_error=True)

        ctx.verdict = self.policy.check(ctx.name, ctx.arguments)
        approved: bool | None = None
        if ctx.verdict.decision is Decision.ASK:
            approved = self.approver(ctx.name, ctx.arguments, ctx.verdict.reason)

        # log-only：权限判定是一条事实，但模型不需要看到它的内部形态。
        # 模型只会看到下面那条 tool/result 的文字。
        session.append(EV_PERMISSION, {
            "call_id": ctx.call_id, "tool": ctx.name,
            "decision": ctx.verdict.decision.value, "reason": ctx.verdict.reason,
            "approved": approved,
        })

        if ctx.verdict.decision is Decision.DENY:
            return ToolResult(f"权限拒绝：{ctx.verdict.reason}。这个操作在本环境中被禁止，请换一种方式。",
                              is_error=True)
        if approved is False:
            return ToolResult("用户拒绝了这次操作。请换一种方式，或者先说明你为什么需要它。", is_error=True)
        return None

    def run_body(self, ctx: ToolCallCtx) -> ToolResult:
        tool = self.registry.get(ctx.name)
        assert tool is not None
        known = set(tool.parameters.get("properties", {}))
        cleaned = {k: v for k, v in ctx.arguments.items() if k in known}
        try:
            return ToolResult(tool.handler(**cleaned))
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"错误：{type(e).__name__}: {e}", is_error=True)

    def post_execute(self, ctx: ToolCallCtx, result: ToolResult) -> ToolResult:
        if len(result.content) > 20000:
            head, tail = result.content[:12000], result.content[-4000:]
            result = ToolResult(f"{head}\n\n…（省略 {len(result.content) - 16000} 字符）…\n\n{tail}",
                                result.is_error)
        return result

    def execute(self, call_id: str, name: str, arguments: dict[str, Any], session: Session) -> ToolResult:
        ctx = ToolCallCtx(call_id=call_id, name=name, arguments=arguments)

        # ────────────────────────────────────────────────────────
        # 在执行**之前**登记。这一条顺序不能反。
        #
        # 如果崩在工具体内，日志里会留下一条没有配对 result 的 tool/call，
        # 恢复时一眼就能看出"这次调用发起了，但没有结论"。
        # 反过来（执行完再记）的话，崩溃现场看起来像"这次调用从未发生"。
        # ────────────────────────────────────────────────────────
        session.append(EV_TOOL_CALL, {"call_id": call_id, "name": name, "arguments": arguments})

        short = self.pre_execute(ctx, session)
        result = short if short is not None else self.run_body(ctx)
        result = self.post_execute(ctx, result)

        session.append(EV_TOOL_RESULT, {
            "call_id": call_id, "name": name,
            "content": result.content, "is_error": result.is_error,
        })
        return result


# ══════════════════════════════════════════════════════════════════
# 沿用 s03/s04（未改动）：六个工具
# ══════════════════════════════════════════════════════════════════

registry = ToolRegistry()
WORKSPACE = Path.cwd()


def safe_path(p: str) -> Path:
    root = WORKSPACE.resolve()
    path = (root / p).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"路径越界，超出工作区：{p}")
    return path


@registry.tool("bash", "在工作目录下执行一条 shell 命令。",
               {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
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


@registry.tool("read", "读取文件内容，返回带行号的文本。",
               {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["path"]})
def run_read(path: str, limit: int | None = None) -> str:
    lines = safe_path(path).read_text(encoding="utf-8").splitlines()
    shown = lines[:limit] if limit else lines
    body = "\n".join(f"{i:>5}  {ln}" for i, ln in enumerate(shown, 1))
    if limit and len(lines) > limit:
        body += f"\n… 还有 {len(lines) - limit} 行未显示"
    return body or "(空文件)"


@registry.tool("write", "写入文件（覆盖已有内容）。",
               {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]})
def run_write(path: str, content: str) -> str:
    f = safe_path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字节）"


@registry.tool("edit", "把文件中某段精确文本替换成新文本。",
               {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                 "new_text": {"type": "string"}},
                "required": ["path", "old_text", "new_text"]})
def run_edit(path: str, old_text: str, new_text: str) -> str:
    f = safe_path(path)
    text = f.read_text(encoding="utf-8")
    if old_text not in text:
        return f"错误：在 {path} 中找不到该文本。先用 read 确认当前内容。"
    f.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已编辑 {path}"


@registry.tool("glob", "按通配符查找文件。",
               {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]})
def run_glob(pattern: str) -> str:
    hits = sorted(globlib.glob(pattern, root_dir=WORKSPACE, recursive=True))
    return "\n".join(hits) if hits else "(无匹配)"


@registry.tool("grep", "在工作区内按子串搜索文件内容。",
               {"type": "object", "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
                "required": ["pattern"]})
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
            continue
    return "\n".join(hits[:100]) if hits else "(无匹配)"


# ══════════════════════════════════════════════════════════════════
# s05 改写：Agent Loop 不再持有 messages
# ══════════════════════════════════════════════════════════════════


def agent_loop(provider, session: Session, executor: ToolExecutor, system: str) -> str:
    """签名变了：`messages: list` → `session: Session`。

    这是全课程唯一一次改动 loop 的**参数**，值得说明为什么值得。

    s04 的 loop 持有 messages，也就是持有"真相"。于是：
      · 想持久化 → 得序列化 loop 的内部状态
      · 想回放   → 没有任何切入点
      · 想知道"第 3 步模型看见了什么" → 那个中间状态早就被覆盖了

    现在 loop 只是**追加事实**，上下文在每次请求前重新算出来。
    loop 从"状态持有者"降级成了"事实生产者" —— 它变简单了，
    而能力反而变强了。
    """
    for _ in range(MAX_STEPS):
        # 每一步都重新投影。不是性能最优，但它保证了一件事：
        # 模型看到的东西，永远等于日志能重建出来的东西。
        # 一旦你为了省事在旁边缓存一份 messages，这个不变量就会悄悄破掉。
        messages = derive_messages(session)

        reply = provider.chat(messages, tools=executor.registry.schemas(), system=system)

        session.append(EV_ASSISTANT_MESSAGE, {
            "text": reply.text,
            "tool_calls": reply.as_assistant_message().get("tool_calls", []),
        })
        if reply.usage:
            session.append(EV_USAGE, dict(reply.usage))   # log-only

        if not reply.wants_tools:
            return reply.text

        for call in reply.tool_calls:
            print(f"  \033[33m→ {call.name}\033[0m \033[90m{_brief(call.arguments)}\033[0m")
            result = executor.execute(call.id, call.name, call.arguments, session)
            mark = "\033[31m✗\033[0m" if result.is_error else "\033[32m✓\033[0m"
            first = result.content[:150].splitlines()[0] if result.content else ""
            print(f"    {mark} \033[90m{first}\033[0m")

    return f"（达到 {MAX_STEPS} 步上限，停止）"


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())


# ══════════════════════════════════════════════════════════════════
# 展示工具
# ══════════════════════════════════════════════════════════════════


def print_event_log(session: Session) -> None:
    print(f"\n\033[1m事件日志（{len(session)} 条，{session.path}）\033[0m")
    for ev in session.events():
        surface = "\033[36mSURFACE\033[0m" if ev.type in SURFACE_EVENTS else "\033[90mlog-only\033[0m"
        detail = {
            EV_USER_MESSAGE: lambda d: d["content"][:52],
            EV_ASSISTANT_MESSAGE: lambda d: (d["text"][:40] or f"(请求 {len(d['tool_calls'])} 个工具)"),
            EV_TOOL_CALL: lambda d: f"{d['name']} {json.dumps(d['arguments'], ensure_ascii=False)[:40]}",
            EV_TOOL_RESULT: lambda d: d["content"][:52].replace("\n", "⏎"),
            EV_PERMISSION: lambda d: f"{d['tool']}: {d['decision']} ({d['reason']})",
            EV_USAGE: lambda d: f"in={d.get('input')} out={d.get('output')}",
            EV_SESSION_START: lambda d: d.get("cwd", ""),
        }.get(ev.type, lambda d: str(d)[:52])
        print(f"  \033[90m#{ev.seq:>2}\033[0m {surface} \033[33m{ev.type:<20}\033[0m {detail(ev.data)}")


def print_messages(messages: list[dict], title: str) -> None:
    print(f"\n\033[1m{title}（{len(messages)} 条）\033[0m")
    for m in messages:
        extra = f" +{len(m['tool_calls'])} tool_calls" if m.get("tool_calls") else ""
        body = str(m.get("content", ""))[:56].replace("\n", "⏎")
        print(f"  \033[35m{m['role']:<9}\033[0m {body}{extra}")


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def make_system(cwd: Path, reg: ToolRegistry) -> str:
    return (f"你是一个编程 Agent，工作目录是 {cwd}。\n"
            f"可用工具：{', '.join(reg.names())}。\n直接动手，不要解释。")


def build_demo_workspace() -> Path:
    d = Path(tempfile.mkdtemp(prefix="s05_demo_"))
    (d / "app.py").write_text('VERSION = "0.1.0"\nprint(VERSION)\n', encoding="utf-8")
    return d


DEMO_PART1 = [
    scripted(calls=[("read", {"path": "app.py"})]),
    scripted(calls=[("edit", {"path": "app.py", "old_text": "0.1.0", "new_text": "0.2.0"})]),
    scripted(calls=[("bash", {"command": "rm -rf ~"})]),
    scripted("版本号已升到 0.2.0。清理环境的命令被环境禁止了。"),
]
DEMO_PART2 = [
    scripted(calls=[("bash", {"command": "cat app.py"})]),
    scripted("我刚才把它从 0.1.0 改成了 0.2.0，现在文件里就是 0.2.0。"),
]


def demo() -> None:
    global WORKSPACE
    WORKSPACE = build_demo_workspace()
    log_path = WORKSPACE / "session.jsonl"
    executor = ToolExecutor(registry, PermissionPolicy(), lambda *a: True)

    # ── 第一段会话 ────────────────────────────────────────────────
    print("\033[1m【第一段】正常跑一轮\033[0m")
    session = Session(path=log_path)
    session.append(EV_SESSION_START, {"cwd": str(WORKSPACE)})
    q1 = "把 app.py 版本号升到 0.2.0，然后清理一下环境"
    print(f"\033[36m你 > \033[0m{q1}")
    session.append(EV_USER_MESSAGE, {"content": q1})
    print(f"\033[32m模型 >\033[0m {agent_loop(get_provider(demo_script=DEMO_PART1), session, executor, make_system(WORKSPACE, registry))}")

    print_event_log(session)
    msgs = derive_messages(session)
    print_messages(msgs, "derive_messages() 投影出的上下文")
    log_only = len(session) - len(msgs)
    print(f"\n\033[90m日志 {len(session)} 条，投影出的消息只有 {len(msgs)} 条 —— "
          f"{log_only} 条 log-only 事件（session/start、tool/call、permission、usage）没有进上下文。\033[0m")

    # ── 模拟进程崩溃 + 恢复 ──────────────────────────────────────
    print("\n\033[1m【第二段】杀掉进程，从磁盘恢复\033[0m")
    del session
    restored = Session.load(log_path)
    print(f"\033[90m从 {log_path.name} 读回 {len(restored)} 条事件\033[0m")

    q2 = "app.py 现在版本号是多少？是你改的吗？"
    print(f"\033[36m你 > \033[0m{q2}")
    restored.append(EV_USER_MESSAGE, {"content": q2})
    print(f"\033[32m模型 >\033[0m {agent_loop(get_provider(demo_script=DEMO_PART2), restored, executor, make_system(WORKSPACE, registry))}")
    print("\n\033[90m模型答得出来，因为恢复后的上下文和崩溃前**逐条一致** ——"
          "\n它不是被存下来的，是从事件重新算出来的。\033[0m")

    # ── 时间旅行 ──────────────────────────────────────────────────
    print("\n\033[1m【第三段】时间旅行：第 5 号事件时，模型看见了什么？\033[0m")
    print_messages(derive_messages(restored, upto=5), "derive_messages(upto=5)")
    print("\n\033[90m这就是 append-only 日志的红利：任意历史时刻的上下文都能精确重算。"
          "\n如果 messages 是真相，这件事根本无从做起 —— 中间状态早被覆盖了。\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章最该记住的一句话：")
    print("  \033[1mModel-visible means logged.\033[0m\033[90m")
    print("  凡是能进模型请求的，都必须能从日志重建。")
    print("  但反过来不成立 —— 日志里有大量东西刻意不给模型看。\033[0m")


def main() -> None:
    global WORKSPACE
    if "--demo" in sys.argv:
        demo()
        return

    if "--replay" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--replay") + 1])
        s = Session.load(p)
        print_event_log(s)
        print_messages(derive_messages(s), "重建出的上下文")
        return

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return

    log_path = Path(f"session_{uuid.uuid4().hex[:8]}.jsonl")
    session = Session(path=log_path)
    session.append(EV_SESSION_START, {"cwd": str(WORKSPACE)})
    executor = ToolExecutor(registry, PermissionPolicy(yolo="--yolo" in sys.argv), cli_approver)

    print("\033[1ms05 — Session Event Log\033[0m")
    print(f"\033[90m日志写入 {log_path}；随时可以 --replay 它\033[0m")
    print("输入问题回车发送，q 退出，/log 查看事件日志。\n")

    system = make_system(WORKSPACE, registry)
    while True:
        try:
            q = input("\033[36m你 > \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("q", "quit", "exit", ""):
            break
        if q == "/log":
            print_event_log(session)
            print_messages(derive_messages(session), "当前上下文")
            continue
        session.append(EV_USER_MESSAGE, {"content": q})
        try:
            print(f"\033[32m模型 >\033[0m {agent_loop(provider, session, executor, system)}\n")
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            break

    print(f"\033[90m会话已持久化：{log_path}（{len(session)} 条事件）\033[0m")


if __name__ == "__main__":
    main()
