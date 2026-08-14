#!/usr/bin/env python3
"""s09 — Subagent（上下文隔离）

    主 Agent 上下文                 子 Agent 上下文（独立、用完即弃）
    ┌────────────────┐             ┌──────────────────────────────┐
    │ user: 鉴权在哪？ │             │ task: 找出鉴权相关文件         │
    │ tool: spawn(…)  │──── spawn ─▶│ grep → 3000 行                │
    │ tool: "结论：    │             │ read → 2000 行                │
    │   auth/mw.py    │◀─── 结论 ───│ read → 1800 行                │
    │   api/deps.py"  │  一句话      │ "结论：auth/mw.py, api/deps"  │
    └────────────────┘             └──────────────────────────────┘
       增长 2 条消息                    7000 行垃圾随子会话一起丢弃

这一章回答：**Subagent 的价值到底是什么？**

不是"多调了一次 LLM"。是 **Context Isolation**：
让昂贵的探索过程发生在一个用完即弃的上下文里，主上下文只收结论。

顺带引入第二个能力：**受限的 action space**。
子 Agent 拿到的工具是主 Agent 的一个**子集**，
而且被过滤掉的工具「既不在它的 prompt 里，也拒绝执行」。

运行：
    python s09_subagent/code.py --demo
    python s09_subagent/code.py --demo --debug
"""

import glob as globlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

MAX_STEPS_PER_TURN = 12


# ══════════════════════════════════════════════════════════════════
# 沿用 s05（未改动）：SessionEvent / Session
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

# ── s06 新增的四个事件 ────────────────────────────────────────────
#
# 它们全是 log-only：turn / step 的边界是 Harness 的结构，
# 不是给模型看的内容。模型只关心消息序列，不关心它被怎么分组。
EV_TURN_START = "turn/start"
EV_TURN_END = "turn/end"
EV_STEP_START = "step/start"
EV_STEP_END = "step/end"

# ── s07 新增 ──────────────────────────────────────────────────────
#
# 组装出来的 system prompt + 工具 schema 也是**请求的一部分**，
# 因此也必须能从日志重建。否则日志能还原 messages 却还原不了
# "模型当时被告知了什么规则"，回放就是残缺的。
#
# 它是 log-only：prompt 不是消息，它是请求的信封。
# 只在**发生变化**时记一条快照（prompt 每步都一样的话记 N 条纯属浪费）。
EV_REQUEST_HEADER = "request/header"

# ── s08 新增 ──────────────────────────────────────────────────────
# 技能正文是通过 inbox 变成 user/message 进上下文的（那条是 SURFACE），
# 这里额外记一条 log-only 事件，用来回答"这一轮为什么突然多了 1100 字"。
EV_SKILL_LOAD = "skill/load"

# ── s09 新增 ──────────────────────────────────────────────────────
# 子 Agent 的**过程**在它自己的日志里；父日志只记"派出去了、拿回来了"。
# 这两条是 log-only：模型看到的是 spawn_agent 的工具结果，
# 不需要再看一遍 Harness 的内部记账。
EV_SUBAGENT_START = "subagent/start"
EV_SUBAGENT_END = "subagent/end"

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
        """从日志里读出当前轮次 —— 而不是在内存里维护一个计数器。

        这不是洁癖。计数器是**第二份真相**：恢复会话时它归零，
        于是新事件的 turn 号会和历史撞车，日志就废了。
        turn 号必须和其他事实一样，从日志推导。
        """
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


def derive_messages(session: Session, upto: int | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for ev in session.events(upto):
        if ev.type not in SURFACE_EVENTS:
            continue
        if ev.type == EV_USER_MESSAGE:
            messages.append({"role": "user", "content": ev.data["content"]})
        elif ev.type == EV_ASSISTANT_MESSAGE:
            msg: dict[str, Any] = {"role": "assistant", "content": ev.data.get("text", "")}
            if ev.data.get("tool_calls"):
                msg["tool_calls"] = ev.data["tool_calls"]
            messages.append(msg)
        elif ev.type == EV_TOOL_RESULT:
            messages.append({"role": "tool", "tool_call_id": ev.data["call_id"], "content": ev.data["content"]})
    return messages


# ══════════════════════════════════════════════════════════════════
# s06 新增：Inbox —— 输入不是"直接进上下文"，是先排队
# ══════════════════════════════════════════════════════════════════


@dataclass
class InboxItem:
    content: str
    source: str = "user"   # user | injected | steering


class Inbox:
    """待认领的输入队列。

    s05 之前，用户输入是**立刻**变成 user/message 的。
    这在单轮问答里没问题，一旦 turn 可能跨越多个 step，问题就来了：

        用户在模型跑到第 3 步时插了一句"等等，先看看 config.py"。
        这句话属于当前这一轮，还是下一轮？

    答案是：它进 inbox 排队，由**下一个 step 认领**（claim）。
    也就是说它属于当前 turn，会立刻影响模型的下一次请求，
    而不是等这一轮全部结束。

    这也解释了 turn 的准确定义：

        turn = 一次输入的**排空**（drain）
        只要 inbox 里还欠着东西，或者工具还欠模型一次请求，这一轮就没结束。

    真实 Harness 里 inbox 还承载注入的上下文（文件变更通知、
    子目录规范、定时任务提醒），来源不同但都走同一条认领路径。
    我们这里保留了 source 字段，s08/s09/s12 会用到。
    """

    def __init__(self) -> None:
        self._q: deque[InboxItem] = deque()

    def put(self, content: str, source: str = "user") -> None:
        self._q.append(InboxItem(content, source))

    def claim(self) -> list[InboxItem]:
        """认领当前排队的所有输入。认领即出队 —— 不会被认领两次。"""
        items = list(self._q)
        self._q.clear()
        return items

    def __bool__(self) -> bool:
        return bool(self._q)


# ══════════════════════════════════════════════════════════════════
# s08 新增：Skill —— 目录常驻，正文按需
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Skill:
    """一份可按需加载的知识。

    两段式结构是这一章的全部：

        summary（name + description）  几十字，**常驻** prompt
        body（SKILL.md 正文）          上千字，**按需**注入

    这个切分不是为了省事，是因为模型需要的信息有两种：
      · "有哪些知识可用" —— 它必须**总是**知道，否则永远想不到去要
      · "某份知识的内容" —— 只在真的要用时才有价值
    """

    name: str
    description: str
    path: Path

    @property
    def body(self) -> str:
        """正文只在被读取时才碰磁盘。

        这一点很重要：如果构造 Skill 时就把正文读进内存，
        那 10 份技能就是 10 份常驻内存，"渐进"只剩了个名字。
        延迟到真的需要时再读，磁盘和上下文的账才是一致的。
        """
        text = self.path.read_text(encoding="utf-8")
        return _strip_frontmatter(text).strip()


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 顶部的 --- name/description --- 块。

    刻意写得很土（不引 yaml 依赖）：技能元数据只有两个字段，
    为它引一个解析库不划算。
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4:] if end >= 0 else text


class SkillRegistry:
    """从磁盘发现技能。

    和 ToolRegistry 是**兄弟关系**，但职责严格不同：

        ToolRegistry   模型能做什么（action space）
        SkillRegistry  模型该怎么做（knowledge）

    把知识做成工具是个常见的错误设计 —— 那样每份知识都会占一个
    tool schema 的位置，而且模型必须"调用"才能读到，无法被搜索、
    无法被组合。技能是**内容**，工具是**能力**。

    真实 Harness 里 SkillRegistry 背后可以有多个 provider
    （本地目录、内置包、远程仓库），我们只有本地目录一个。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._skills: dict[str, Skill] = {}
        self.discover()

    def discover(self) -> None:
        self._skills.clear()
        if not self.root.exists():
            return
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            meta = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            name = meta.get("name") or skill_file.parent.name
            desc = meta.get("description", "")
            if not desc:
                # 没有 description 的技能对模型是不可见的 —— 它无从判断
                # 什么时候该用。宁可跳过并出声，也不要塞一份模型永远想不到用的东西。
                print(f"\033[33m[skill] 跳过 {name}：缺少 description\033[0m")
                continue
            self._skills[name] = Skill(name=name, description=desc, path=skill_file)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def catalog(self) -> list[tuple[str, str]]:
        """常驻 prompt 的那一部分：只有名字和一句话。"""
        return [(s.name, s.description) for s in self._skills.values()]


# ══════════════════════════════════════════════════════════════════
# s07 新增：Prompt Assembly
# ══════════════════════════════════════════════════════════════════


@dataclass
class RuntimeContext:
    """组装 prompt 时能看到的运行时事实。

    section 只能读它，不能读全局变量 —— 这条约束让每个 section
    都可以被单独测试：给一个假的 RuntimeContext，断言它渲染出什么。

    它会随着章节增长（s08 加 skills、s11 加 tasks、s12 加 jobs），
    但增长的是**数据**，不是 assemble() 的逻辑。这正是重点。
    """

    cwd: Path
    tool_names: list[str]
    turn: int = 0
    step: int = 0
    project_notes: str | None = None       # AGENTS.md / CLAUDE.md 之类
    files_read: list[str] = field(default_factory=list)
    # ── s08 新增 ──
    skill_catalog: list[tuple[str, str]] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    # ── s09 新增 ──
    subagent_presets: list["SubagentPreset"] = field(default_factory=list)


@dataclass(frozen=True)
class PromptSection:
    """system prompt 的一块。

    三个字段就够了：

      name    身份，用于替换/移除（s14 的插件要靠它覆盖别人的 section）
      order   排序权重，小的在前
      render  (ctx) -> str | None；返回 None 表示**这次不出现**

    最后那条是整章的关键。s06 的 f-string 里，每一块都是无条件拼进去的：
    没有项目规范也要写一行"项目规范：无"，纯属浪费 token。
    render 返回 None，这一块就干脆消失。

        prompt 不是模板填空，是**按当前状态挑选内容**。
    """

    name: str
    order: int
    render: Callable[[RuntimeContext], str | None]


class SystemPromptRegistry:
    """按 order 排序，逐块渲染，跳过 None，拼起来。

    整个类只有二十几行 —— 这是刻意的。
    它的价值不在于逻辑复杂，而在于**它把"谁能往 prompt 里加东西"
    这件事变成了一个注册动作**。

    s06 的写法下，加一段 prompt 要去改 make_system() 那个函数；
    到 s14 有了插件系统之后，那个函数会变成所有插件的争抢点。
    现在插件只需要 register 一个 section，谁也不用碰别人的代码。
    """

    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def register(self, section: PromptSection) -> None:
        self._sections[section.name] = section     # 同名覆盖 = 替换

    def section(self, name: str, order: int) -> Callable:
        def deco(fn: Callable[[RuntimeContext], str | None]):
            self.register(PromptSection(name, order, fn))
            return fn
        return deco

    def remove(self, name: str) -> None:
        self._sections.pop(name, None)

    def names(self) -> list[str]:
        return [s.name for s in sorted(self._sections.values(), key=lambda s: s.order)]

    def assemble(self, ctx: RuntimeContext) -> str:
        parts: list[str] = []
        for sec in sorted(self._sections.values(), key=lambda s: s.order):
            text = sec.render(ctx)
            if text:                       # None 或空字符串都跳过
                parts.append(text.strip())
        return "\n\n".join(parts)

    def explain(self, ctx: RuntimeContext) -> list[tuple[str, int]]:
        """调试用：这次组装里，每个 section 贡献了多少字符。"""
        out = []
        for sec in sorted(self._sections.values(), key=lambda s: s.order):
            text = sec.render(ctx) or ""
            out.append((sec.name, len(text.strip())))
        return out


prompts = SystemPromptRegistry()


@prompts.section("identity", 10)
def _identity(ctx: RuntimeContext) -> str:
    return "你是一个编程 Agent。直接动手完成任务，不要先解释你打算怎么做。"


@prompts.section("environment", 20)
def _environment(ctx: RuntimeContext) -> str:
    return f"# 环境\n工作目录：{ctx.cwd}\n所有文件操作都被限制在这个目录内。"


@prompts.section("project", 30)
def _project(ctx: RuntimeContext) -> str | None:
    # 条件性 section 的典型例子：没有 AGENTS.md 就一个字都不占。
    # s06 的 f-string 做不到这一点 —— 它只能拼一句"项目规范：无"。
    if not ctx.project_notes:
        return None
    return f"# 项目约定\n{ctx.project_notes.strip()}"


@prompts.section("tools", 40)
def _tools(ctx: RuntimeContext) -> str:
    # 只写"怎么用"，不写"有哪些参数" —— 参数在 tool schema 里，
    # 由 provider 单独发送。在 prompt 里重复一遍是常见的浪费，
    # 而且两处会漂移（和 s03 讲的 schema/实现漂移是同一个病）。
    return (f"# 工具\n可用工具：{', '.join(ctx.tool_names)}\n"
            "读文件优先用 read 而不是 bash cat；查找用 glob/grep 而不是 find。")


@prompts.section("skills", 45)
def _skills(ctx: RuntimeContext) -> str | None:
    """常驻的**目录**，不是正文。

    这一块的大小和技能数量成正比，但和技能**内容**的长度无关 ——
    3 份技能 1100 字的正文，在这里只占约 150 字。

    最后那句提示很关键：如果只列目录而不告诉模型"可以用 skill 工具加载"，
    模型会以为这就是全部内容，永远不会去要正文。
    渐进式披露的两半（目录 + 取用方式）必须一起给。
    """
    if not ctx.skill_catalog:
        return None
    lines = [f"- {name}：{desc}" for name, desc in ctx.skill_catalog]
    loaded = f"\n已加载：{', '.join(ctx.loaded_skills)}" if ctx.loaded_skills else ""
    return ("# 可用技能\n"
            "下面是可按需加载的知识。**只列了标题**，需要时用 skill 工具加载全文。\n"
            + "\n".join(lines) + loaded)


@prompts.section("subagents", 46)
def _subagents(ctx: RuntimeContext) -> str | None:
    """把可用的子 Agent 类型摆出来，让模型自己选。

    注意这里只描述**能力**（"只读探索"/"可读写"），
    不写"什么时候该用哪个" —— 那是模型的判断。
    """
    if not ctx.subagent_presets:
        return None
    lines = [f"- {p.name}：{p.description}（工具：{', '.join(p.tools)}）" for p in ctx.subagent_presets]
    return ("# 子 Agent\n"
            "可以用 spawn_agent 把一项独立子任务交给它们。子 Agent 有自己的上下文，\n"
            "看不到你的对话历史，只会把结论返回给你。\n"
            "适合「过程会产生大量中间内容、但你只需要结果」的任务。\n" + "\n".join(lines))


@prompts.section("session_state", 50)
def _session_state(ctx: RuntimeContext) -> str | None:
    """随会话推进而变化的一块 —— 证明 prompt 是运行时产物。

    它每一步都可能不同：读过的文件越多，这一段越长。
    如果 prompt 是常量，这种"让模型知道自己已经做过什么"的能力就没有。
    """
    if not ctx.files_read:
        return None
    # 注意这里**不写步号**。步号每步都变，会让 prompt 每步都不同，
    # 于是 request/header 快照失去去重、provider 端的 prompt 缓存也全部失效。
    # 只放模型真正需要的信息：它已经读过哪些文件。
    return (f"# 当前进度（第 {ctx.turn} 轮）\n"
            f"本轮已读取：{', '.join(ctx.files_read)}\n不要重复读同一个文件。")


# ══════════════════════════════════════════════════════════════════
# s06 新增：Tracer —— 把 Harness 内部正在发生的事情显示出来
# ══════════════════════════════════════════════════════════════════


class Tracer:
    """Harness 是抽象系统，看不见就学不会。

    刻意做成一个**独立对象**而不是散落的 print：
    s13 会把它整个换成一个事件监听器，那时 loop 里的 tracer 调用会消失。
    先让它有形状，才好在后面把它拆走。
    """

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
            print(f"  \033[34m[step {step}]\033[0m", end="")
            print(f"  claimed={len(claimed)}" + (f" ({claimed[0].source})" if claimed else ""))

    def request(self, messages: list, tools: list, system: str) -> None:
        if self.enabled:
            print(f"    \033[90m→ model request   messages={len(messages)} tools={len(tools)} "
                  f"system={len(system)}chars\033[0m")

    def reply(self, reply) -> None:
        if self.enabled:
            names = ",".join(c.name for c in reply.tool_calls) or "-"
            print(f"    \033[90m← model reply     text={len(reply.text)}chars "
                  f"tool_calls={len(reply.tool_calls)} [{names}] "
                  f"usage={reply.usage.get('input', 0)}/{reply.usage.get('output', 0)}\033[0m")

    def tool_pre(self, name: str, args: dict, verdict: str) -> None:
        if self.enabled:
            print(f"    \033[90m· tool pre        {name} {_brief(args)[:60]} → {verdict}\033[0m")

    def tool_result(self, name: str, result: "ToolResult") -> None:
        if self.enabled:
            state = "error" if result.is_error else "ok"
            print(f"    \033[90m· tool result     {name} {state} {len(result.content)}B\033[0m")

    def step_end(self, turn: int, step: int) -> None:
        if self.enabled:
            print(f"  \033[34m[step {step} end]\033[0m")


# ══════════════════════════════════════════════════════════════════
# 沿用 s03–s05（未改动）：Tool / ToolResult / ToolRegistry / 权限
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

    # ── s09 新增 ──────────────────────────────────────────────────
    def restricted(self, allowed: list[str]) -> "ToolRegistry":
        """派生一个只含指定工具的注册表。

        这是 s03 那句"registry 是模型行动空间的**唯一**来源"开始还债的时刻。

        因为 schemas() 和 get() 来自同一个 dict，被过滤掉的工具会同时：
          · 不出现在子 Agent 的 prompt 里（它根本不知道有这个工具）
          · 执行时也找不到（就算模型硬猜出名字也调不动）

        两者一致，才叫真的"限制"。如果只是在 prompt 里不写、执行时照样能跑，
        那就只是"没告诉它"，不是"不允许"。
        """
        sub = ToolRegistry()
        for name in allowed:
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"限制列表里有不存在的工具：{name}")
            sub._tools[name] = tool      # 共享同一个 Tool 对象，不复制实现
        return sub


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
# 沿用 s05（仅加入 tracer 回调）：ToolExecutor
# ══════════════════════════════════════════════════════════════════


@dataclass
class ToolCallCtx:
    call_id: str
    name: str
    arguments: dict[str, Any]
    turn: int
    step: int
    verdict: Verdict | None = None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PermissionPolicy,
                 approver: Approver, tracer: Tracer) -> None:
        self.registry = registry
        self.policy = policy
        self.approver = approver
        self.tracer = tracer

    def pre_execute(self, ctx: ToolCallCtx, session: Session) -> ToolResult | None:
        tool = self.registry.get(ctx.name)
        if tool is None:
            self.tracer.tool_pre(ctx.name, ctx.arguments, "unknown-tool")
            return ToolResult(f"错误：没有名为 '{ctx.name}' 的工具。可用工具：{', '.join(self.registry.names())}",
                              is_error=True)
        missing = [k for k in tool.required if k not in ctx.arguments]
        if missing:
            self.tracer.tool_pre(ctx.name, ctx.arguments, "missing-args")
            return ToolResult(f"错误：{ctx.name} 缺少必填参数：{', '.join(missing)}", is_error=True)

        ctx.verdict = self.policy.check(ctx.name, ctx.arguments)
        approved: bool | None = None
        if ctx.verdict.decision is Decision.ASK:
            approved = self.approver(ctx.name, ctx.arguments, ctx.verdict.reason)

        self.tracer.tool_pre(ctx.name, ctx.arguments,
                             ctx.verdict.decision.value + ("" if approved is None else f"→{'y' if approved else 'n'}"))
        session.append(EV_PERMISSION, {
            "turn": ctx.turn, "step": ctx.step, "call_id": ctx.call_id, "tool": ctx.name,
            "decision": ctx.verdict.decision.value, "reason": ctx.verdict.reason, "approved": approved,
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
        self.tracer.tool_result(ctx.name, result)
        return result

    def execute(self, call_id: str, name: str, arguments: dict[str, Any],
                session: Session, turn: int, step: int) -> ToolResult:
        ctx = ToolCallCtx(call_id, name, arguments, turn, step)
        # 所有 tool 事件现在都带 turn/step 坐标 —— 日志从"平的"变成"有层次的"
        session.append(EV_TOOL_CALL, {"turn": turn, "step": step, "call_id": call_id,
                                      "name": name, "arguments": arguments})
        short = self.pre_execute(ctx, session)
        result = short if short is not None else self.run_body(ctx)
        result = self.post_execute(ctx, result)
        session.append(EV_TOOL_RESULT, {"turn": turn, "step": step, "call_id": call_id,
                                        "name": name, "content": result.content, "is_error": result.is_error})
        return result


# ══════════════════════════════════════════════════════════════════
# 沿用 s03–s05（未改动）：六个工具
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


# ── s08 新增：skill 工具 ──────────────────────────────────────────
#
# 这两个全局变量由 main()/demo() 装配。做成全局是为了让工具 handler
# 保持"普通函数"的样子；s14 有了插件上下文之后它们会被正式安置。
SKILLS: "SkillRegistry | None" = None
INBOX: "Inbox | None" = None
RT: "RuntimeContext | None" = None


@registry.tool("skill", "加载一份技能的完整内容。只在你确实需要那份知识时调用。",
               {"type": "object",
                "properties": {"name": {"type": "string", "description": "技能名，见 system prompt 的可用技能列表"}},
                "required": ["name"]})
def run_skill(name: str) -> str:
    """加载技能正文。

    注意它**不把正文作为工具结果返回**，而是注入 inbox。为什么？

    工具结果是 role:"tool" 的消息，它在语义上是"某次调用的输出"，
    模型会把它当成一个**观察**（"我执行了 X，得到了 Y"）。
    而技能正文是**指令**，它应该像用户说的话一样有约束力。

    走 inbox 注入成 user 消息，还有一个好处：它复用了 s06 已经建好的
    认领机制。技能内容、后台任务完成通知、文件变更提醒……
    这些"Harness 想让模型知道的事"来源各异，但只有一条入口。

    工具本身只返回一句确认 —— 真正的内容在下一步才进上下文。
    """
    assert SKILLS is not None and INBOX is not None and RT is not None
    skill = SKILLS.get(name)
    if skill is None:
        return f"错误：没有名为 '{name}' 的技能。可用：{', '.join(SKILLS.names())}"
    if name in RT.loaded_skills:
        return f"技能 {name} 已经加载过了，内容就在上文，不要重复加载。"

    RT.loaded_skills.append(name)
    body = skill.body
    INBOX.put(f"[已加载技能：{name}]\n\n{body}", source="skill")
    return f"已加载技能 {name}（{len(body)} 字符），内容将在下一步进入上下文。"


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
# s09 新增：Subagent
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SubagentPreset:
    """一类子 Agent 的定义。

    Harness 定义**能力信封**（有哪些角色、各自能用什么工具），
    模型决定**用哪个信封干什么活**。

    这条分工很重要，也很容易搞错。反面写法长这样：

        if "搜索" in task: spawn("explorer")        # ❌ Harness 在替模型判断

    我们的写法是：把 explorer / editor 两个 preset 摆进 prompt，
    模型自己读、自己选。Harness 从头到尾不知道任务是什么类型。

    为什么不干脆让模型自己指定工具列表（tools=["bash","write"]）？
    因为那等于让模型给自己发权限。**安全边界必须由 Harness 划**，
    模型只能在已划好的边界里选一个。
    """

    name: str
    description: str
    tools: list[str]
    identity: str


SUBAGENT_PRESETS: dict[str, SubagentPreset] = {
    "explorer": SubagentPreset(
        name="explorer",
        description="只读探索。适合大范围搜索、通读代码、定位问题，会返回一段结论。",
        tools=["read", "glob", "grep"],
        identity=("你是一个只读探索子 Agent。你只能读，不能改任何东西。\n"
                  "把任务查清楚，然后用**尽量短**的一段话汇报结论。\n"
                  "汇报里要给出具体的文件名和行号，不要粘贴大段原文 —— "
                  "主 Agent 只需要结论和线索，不需要你看过的所有内容。"),
    ),
    "editor": SubagentPreset(
        name="editor",
        description="可读写。适合把一个已经明确的改动落地并自验证，会返回改了什么。",
        tools=["read", "glob", "grep", "edit", "write", "bash"],
        identity=("你是一个执行子 Agent。任务已经明确，把它做完并自己验证。\n"
                  "完成后简短汇报：改了哪些文件、验证结果是什么。"),
    ),
}

MAX_SUBAGENT_DEPTH = 1   # 资源保护：子 Agent 不能再 spawn，避免无限套娃

# 这些全局变量由 main()/demo() 装配。
# 它们的存在本身就是一个信号：模块级全局正在变多，装配逻辑开始散落。
# s14 的插件上下文会把它们收编。
PROVIDER_FOR_SUBAGENT: "Callable[[], Any] | None" = None
PARENT_SESSION: "Session | None" = None
TRACER: "Tracer | None" = None
SUBAGENT_DEPTH = 0


@registry.tool(
    "spawn_agent",
    "派一个子 Agent 去完成一项独立的子任务。它在自己的上下文里工作，只把结论返回给你。"
    "适合那种「过程会产生大量中间内容、但你只需要结果」的任务。",
    {"type": "object",
     "properties": {
         "agent": {"type": "string", "description": "子 Agent 类型，见 system prompt 里的列表"},
         "task": {"type": "string", "description": "交给它的任务描述。要写清楚，它看不到你的对话历史。"},
     },
     "required": ["agent", "task"]},
)
def run_spawn(agent: str, task: str) -> str:
    """派生一个子 Agent，跑完，只返回结论。

    这个函数是整章的核心，值得逐段看。
    """
    global SUBAGENT_DEPTH
    assert PROVIDER_FOR_SUBAGENT is not None and PARENT_SESSION is not None and TRACER is not None

    preset = SUBAGENT_PRESETS.get(agent)
    if preset is None:
        return f"错误：没有 '{agent}' 这种子 Agent。可选：{', '.join(SUBAGENT_PRESETS)}"
    if SUBAGENT_DEPTH >= MAX_SUBAGENT_DEPTH:
        return "错误：子 Agent 不能再派生子 Agent。请自己完成这项任务。"

    # ── ① 独立的 Session ─────────────────────────────────────────
    #
    # 子 Agent 有**自己的事件日志**，不是往父日志里追加。
    # 这就是"隔离"的物理实现：它的 grep 结果、read 内容、走过的弯路
    # 全部落在另一个文件里，主上下文的 derive_messages() 永远看不到。
    child_path = (PARENT_SESSION.path.parent / f"{PARENT_SESSION.id}_sub_{uuid.uuid4().hex[:6]}.jsonl"
                  if PARENT_SESSION.path else None)
    child = Session(path=child_path)
    child.append(EV_SESSION_START, {"cwd": str(WORKSPACE), "parent": PARENT_SESSION.id, "preset": agent})

    # ── ② 受限的行动空间 ─────────────────────────────────────────
    #
    # restricted() 保证被过滤掉的工具既不在它的 prompt 里，也拒绝执行。
    # explorer 拿不到 write/edit/bash —— 它在结构上就改不了任何东西，
    # 不需要靠"我们叮嘱过它别改"。
    child_registry = registry.restricted(preset.tools)
    child_executor = ToolExecutor(child_registry, PermissionPolicy(), lambda *a: True, TRACER)

    # ── ③ 独立的 prompt ──────────────────────────────────────────
    #
    # 子 Agent 有自己的 identity，而且**不继承**主 Agent 的技能目录
    # 和项目进度。它是一个新生的 Agent，只知道任务本身。
    child_rt = RuntimeContext(cwd=WORKSPACE, tool_names=child_registry.names(),
                              project_notes=None, skill_catalog=[])
    child_prompts = SystemPromptRegistry()
    child_prompts.register(PromptSection("identity", 10, lambda c: preset.identity))
    child_prompts.register(PromptSection("environment", 20, _environment))
    child_prompts.register(PromptSection("tools", 40, _tools))

    child_inbox = Inbox()
    child_inbox.put(task)

    PARENT_SESSION.append(EV_SUBAGENT_START, {
        "child_session": child.id, "preset": agent, "task": task[:500],
        "tools": child_registry.names(),
    })
    # 这个视觉边界不是装饰：不标出来的话，子 Agent 的工具调用会和主 Agent 的
    # 混在同一片输出里，读者会误以为它们在同一个上下文。
    print(f"    \033[95m┌─ subagent[{agent}] 启动  tools={','.join(child_registry.names())}\033[0m")

    SUBAGENT_DEPTH += 1
    try:
        outcome = run_turn(PROVIDER_FOR_SUBAGENT(), child, child_executor, child_rt,
                           child_inbox, TRACER, prompt_registry=child_prompts)
    finally:
        SUBAGENT_DEPTH -= 1

    child_msgs = len(derive_messages(child))
    child_tokens = sum(e.data.get("input", 0) for e in child.events() if e.type == EV_USAGE)

    PARENT_SESSION.append(EV_SUBAGENT_END, {
        "child_session": child.id, "preset": agent,
        "steps": outcome.steps, "child_messages": child_msgs,
        "child_input_tokens": child_tokens, "result_chars": len(outcome.text),
    })
    print(f"    \033[95m└─ subagent[{agent}] 结束  steps={outcome.steps}  "
          f"子上下文 {child_msgs} 条消息 → 只返回 {len(outcome.text)} 字符\033[0m")

    # ── ④ 只把结论带回来 ─────────────────────────────────────────
    #
    # 子会话里的 N 条消息**全部丢弃**，父上下文只多了这一个工具结果。
    # 这才是 subagent 的真正价值。
    return outcome.text or "（子 Agent 没有返回结论）"


# ══════════════════════════════════════════════════════════════════
# s06 改写：agent_loop → run_turn，显式的 Turn / Step 结构
# ══════════════════════════════════════════════════════════════════


@dataclass
class TurnOutcome:
    turn: int
    steps: int
    reason: str
    text: str


def run_turn(provider, session: Session, executor: ToolExecutor, rt: RuntimeContext,
             inbox: Inbox, tracer: Tracer,
             prompt_registry: "SystemPromptRegistry | None" = None) -> TurnOutcome:
    """跑完一个 turn。

    turn 的定义（和工业 Harness 一致）：

        turn = 一次**输入排空**（drain）。
               它在认领第一批输入之前开启，在"什么都不欠了"之后关闭。

        step = 一次模型请求 + 它引发的工具执行。
               一个 turn 包含**零个或多个** step。

    "零个"不是理论上的边角情况：turn 开了但输入被过滤空了 / 被拒绝了 /
    被取消了，都会得到一个没有 step 的 turn。这条日志仍然有价值 ——
    它记录了"有一次尝试发生过但没进模型"。

    这一轮什么时候继续？两个条件，满足任一就再来一个 step：

        · 模型还要工具（工具欠模型一次请求）
        · inbox 里又来了新输入（用户中途插话）

    注意这两个条件都**不是**在判断任务内容。Harness 仍然不知道
    这是 debug 任务还是写文档任务。
    """
    turn = session.last_turn() + 1
    session.append(EV_TURN_START, {"turn": turn})
    tracer.turn_start(turn)

    step = 0
    reason = "natural-stop"
    final_text = ""
    last_header: str | None = None    # 上一次记进日志的 prompt，用于去重
    rt.turn = turn
    rt.files_read = []                # 进度类信息按轮重置
    # loaded_skills **不**重置：技能正文一旦注入就永久留在上下文里
    # （它是一条 user/message，日志里抹不掉），重置只会诱导模型重复加载。

    while True:
        claimed = inbox.claim()

        # 没有任何输入可认领，而且这一轮还没跑过 step → 空 turn
        if step == 0 and not claimed:
            reason = "no-input"
            break

        step += 1
        session.append(EV_STEP_START, {"turn": turn, "step": step})
        tracer.step_start(turn, step, claimed)

        # 认领到的输入，现在才变成 user/message 事件。
        # 从 s05 的"输入立刻进日志"改成"输入先排队、被 step 认领时才进日志" ——
        # 这样日志里 user/message 的位置就精确表达了"模型在第几步看到它"。
        for item in claimed:
            session.append(EV_USER_MESSAGE, {"turn": turn, "step": step,
                                             "content": item.content, "source": item.source})

        # ── s07：prompt 在**每一步**重新组装 ─────────────────────
        #
        # 不是每轮一次，是每步一次。因为 session_state 这类 section
        # 的内容会在同一轮内变化（刚读完一个文件，下一步就该知道）。
        #
        # 组装是纯函数：assemble(ctx) 的结果只取决于 ctx，
        # 所以它便宜、可测、可预测。
        rt.step = step
        rt.tool_names = executor.registry.names()
        if SKILLS is not None and prompt_registry is None:
            rt.skill_catalog = SKILLS.catalog()
        # s09：子 Agent 用自己的 section 集合。
        # run_turn 本身完全不知道"谁是子 Agent" —— 它只是拿到一个不同的注册表。
        pr = prompt_registry or prompts
        system = pr.assemble(rt)

        messages = derive_messages(session)
        tools = executor.registry.schemas()
        tracer.request(messages, tools, system)

        # 只在 prompt 发生**变化**时记一条快照。
        # 记全量太吵，不记则日志无法重建请求 —— 变化时记是两者的交点。
        if system != last_header:
            session.append(EV_REQUEST_HEADER, {
                "turn": turn, "step": step,
                "system": system,
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

            # 把执行结果反馈到 RuntimeContext，下一步组装 prompt 时就能用上。
            # 这条线是"prompt 是运行时产物"最直观的证据：
            # 工具执行改变了 ctx，ctx 改变了 prompt，prompt 改变了模型看到的内容。
            if call.name == "read" and not result.is_error:
                p = str(call.arguments.get("path", ""))
                if p and p not in rt.files_read:
                    rt.files_read.append(p)
            if call.name == "skill" and not result.is_error:
                session.append(EV_SKILL_LOAD, {"turn": turn, "step": step,
                                               "name": str(call.arguments.get("name", ""))})

            if not tracer.enabled:
                mark = "\033[31m✗\033[0m" if result.is_error else "\033[32m✓\033[0m"
                first = result.content[:140].splitlines()[0] if result.content else ""
                print(f"{indent}  {mark} \033[90m{first}\033[0m")

        session.append(EV_STEP_END, {"turn": turn, "step": step})
        tracer.step_end(turn, step)

        if reply.text:
            final_text = reply.text

        # ── 这一轮还欠着东西吗？ ──────────────────────────────────
        if reply.wants_tools:
            pass                      # 工具结果欠模型一次请求 → 继续
        elif inbox:
            pass                      # 用户中途插话 → 同一轮里继续
        else:
            reason = "natural-stop"   # 什么都不欠了 → 收工
            break

        if step >= MAX_STEPS_PER_TURN:
            reason = "max-steps"
            break

    session.append(EV_TURN_END, {"turn": turn, "reason": reason, "steps": step})
    tracer.turn_end(turn, reason, step)
    return TurnOutcome(turn, step, reason, final_text)


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:50]!r}" for k, v in args.items())


# ══════════════════════════════════════════════════════════════════
# 展示
# ══════════════════════════════════════════════════════════════════


def print_turn_tree(session: Session) -> None:
    """把平坦的事件日志渲染成 turn/step 树。

    这件事在 s05 是**做不到**的：日志里没有层次信息，
    你无法知道第 7 号事件属于第几步。
    """
    print("\n\033[1m会话结构（从事件日志重建）\033[0m")
    for ev in session.events():
        d = ev.data
        if ev.type == EV_TURN_START:
            print(f"\033[1;34mTurn {d['turn']}\033[0m")
        elif ev.type == EV_STEP_START:
            print(f"  \033[34m├── Step {d['step']}\033[0m")
        elif ev.type == EV_USER_MESSAGE:
            print(f"  │     \033[36muser({d.get('source', 'user')})\033[0m  {d['content'][:46]}")
        elif ev.type == EV_ASSISTANT_MESSAGE:
            what = d["text"][:40] or f"(请求 {len(d['tool_calls'])} 个工具)"
            print(f"  │     \033[32mmodel\033[0m       {what}")
        elif ev.type == EV_TOOL_CALL:
            print(f"  │     \033[33mtool call\033[0m   {d['name']}")
        elif ev.type == EV_TOOL_RESULT:
            print(f"  │     \033[33mtool result\033[0m {d['content'][:40].splitlines()[0] if d['content'] else ''}")
        elif ev.type == EV_TURN_END:
            print(f"  \033[1;34m└── Turn {d['turn']} end\033[0m  reason={d['reason']} steps={d['steps']}")


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def load_project_notes(cwd: Path) -> str | None:
    """项目约定文件。有就读，没有就 None（对应的 section 会整块消失）。"""
    for name in ("AGENTS.md", "CLAUDE.md", ".agent.md"):
        f = cwd / name
        if f.exists():
            return f.read_text(encoding="utf-8")[:4000]
    return None


def build_demo_workspace(with_notes: bool = True) -> Path:
    d = Path(tempfile.mkdtemp(prefix="s07_demo_"))
    (d / "app.py").write_text('VERSION = "0.1.0"\n\ndef main():\n    print(VERSION)\n', encoding="utf-8")
    (d / "config.py").write_text("DEBUG = True\nTIMEOUT = 30\n", encoding="utf-8")
    if with_notes:
        (d / "AGENTS.md").write_text(
            "- 版本号统一写在 app.py 顶部的 VERSION 常量里\n"
            "- 改完代码必须运行 `python3 app.py` 验证\n", encoding="utf-8")
    return d


def print_prompt_breakdown(rt: RuntimeContext, title: str) -> None:
    rows = prompts.explain(rt)
    total = sum(size for _, size in rows)
    print(f"\n\033[1m{title}\033[0m  \033[90m（共 {total} 字符）\033[0m")
    for name, size in rows:
        if size:
            bar = "█" * max(1, size // 12)
            print(f"  \033[36m{name:<14}\033[0m {size:>4}  \033[90m{bar}\033[0m")
        else:
            print(f"  \033[90m{name:<14}    -  （本次不出现）\033[0m")


def build_big_workspace() -> Path:
    """造一个「搜索会产生大量中间内容」的工作区。

    12 个文件，每个几十行 —— 足够让 grep+read 的中间产物远大于结论。
    """
    d = Path(tempfile.mkdtemp(prefix="s09_demo_"))
    (d / "auth").mkdir()
    (d / "api").mkdir()
    (d / "util").mkdir()
    (d / "auth" / "middleware.py").write_text(
        "\n".join(["import hashlib", "", "SECRET = 'demo'", "", "def verify_token(token: str) -> bool:",
                   "    # 鉴权入口：所有请求都过这里", "    return bool(token)", ""] +
                  [f"def _helper_{i}(): return {i}" for i in range(30)]), encoding="utf-8")
    (d / "api" / "deps.py").write_text(
        "\n".join(["from auth.middleware import verify_token", "",
                   "def current_user(token):", "    # 鉴权依赖注入", "    return verify_token(token)", ""] +
                  [f"ROUTE_{i} = '/x{i}'" for i in range(30)]), encoding="utf-8")
    for i in range(10):
        (d / "util" / f"mod{i}.py").write_text(
            "\n".join([f"# util module {i}"] + [f"def f{j}(): return {j}" for j in range(25)]), encoding="utf-8")
    (d / "AGENTS.md").write_text("- 鉴权改动必须同时更新 auth/ 和 api/ 两处\n", encoding="utf-8")
    return d


def demo(debug: bool) -> None:
    global WORKSPACE, SKILLS, INBOX, RT, PROVIDER_FOR_SUBAGENT, PARENT_SESSION, TRACER
    WORKSPACE = build_big_workspace()
    SKILLS = SkillRegistry(Path(__file__).resolve().parent / "skills")
    TRACER = Tracer(enabled=debug)
    executor = ToolExecutor(registry, PermissionPolicy(), lambda *a: True, TRACER)
    session = Session(path=WORKSPACE / "session.jsonl")
    PARENT_SESSION = session
    session.append(EV_SESSION_START, {"cwd": str(WORKSPACE)})
    INBOX = Inbox()
    RT = RuntimeContext(cwd=WORKSPACE, tool_names=registry.names(),
                        project_notes=load_project_notes(WORKSPACE),
                        skill_catalog=SKILLS.catalog(),
                        subagent_presets=list(SUBAGENT_PRESETS.values()))

    # ── 对照组：不用子 Agent，全在主上下文里搜 ────────────────────
    print("\033[1m【对照组】不用子 Agent：所有搜索结果都进主上下文\033[0m")
    flat_session = Session()
    flat_session.append(EV_SESSION_START, {"cwd": str(WORKSPACE)})
    flat_inbox = Inbox()
    flat_inbox.put("这个代码库里，鉴权逻辑分散在哪些文件？")
    flat_rt = RuntimeContext(cwd=WORKSPACE, tool_names=registry.names())
    flat_script = [
        scripted(calls=[("grep", {"pattern": "def "})]),
        scripted(calls=[("read", {"path": "auth/middleware.py"})]),
        scripted(calls=[("read", {"path": "api/deps.py"})]),
        scripted("鉴权在 auth/middleware.py 的 verify_token 和 api/deps.py 的 current_user。"),
    ]
    flat_out = run_turn(get_provider(demo_script=flat_script), flat_session, executor,
                        flat_rt, flat_inbox, TRACER)
    flat_chars = sum(len(str(m.get("content", ""))) for m in derive_messages(flat_session))
    print(f"\033[32m模型 >\033[0m {flat_out.text}")
    print(f"\033[31m  主上下文：{len(derive_messages(flat_session))} 条消息 / {flat_chars} 字符"
          f"  ← 这些会跟着你走完整个会话\033[0m")

    # ── 实验组：派 explorer 去搜 ─────────────────────────────────
    print("\n\033[1m【实验组】派一个只读 explorer 去搜，主上下文只收结论\033[0m")
    q = "这个代码库里，鉴权逻辑分散在哪些文件？"
    print(f"\033[36m你 > \033[0m{q}")
    INBOX.put(q)

    # 子 Agent 的"模型"：它在自己的上下文里做同样的搜索
    def subagent_provider():
        return get_provider(demo_script=[
            scripted(calls=[("grep", {"pattern": "def "})]),
            scripted(calls=[("read", {"path": "auth/middleware.py"})]),
            scripted(calls=[("read", {"path": "api/deps.py"})]),
            scripted("鉴权集中在两处：auth/middleware.py:5 verify_token（入口），"
                     "api/deps.py:3 current_user（依赖注入）。util/ 下的 10 个模块与鉴权无关。"),
        ])
    PROVIDER_FOR_SUBAGENT = subagent_provider

    main_script = [
        scripted(calls=[("spawn_agent", {"agent": "explorer",
                                         "task": "在这个代码库里找出所有和鉴权相关的文件与函数，给出文件名和行号。"})]),
        scripted("鉴权在 auth/middleware.py:5 的 verify_token 和 api/deps.py:3 的 current_user。"),
    ]
    out = run_turn(get_provider(demo_script=main_script), session, executor, RT, INBOX, TRACER)
    print(f"\033[32m模型 >\033[0m {out.text}")

    main_chars = sum(len(str(m.get("content", ""))) for m in derive_messages(session))
    end_ev = next(e for e in session.events() if e.type == EV_SUBAGENT_END)
    print(f"\033[32m  主上下文：{len(derive_messages(session))} 条消息 / {main_chars} 字符\033[0m")
    print(f"\033[90m  子上下文：{end_ev.data['child_messages']} 条消息（已随子会话丢弃）\033[0m")
    print(f"\033[1m  主上下文省下了 {flat_chars - main_chars} 字符"
          f"（{100 - main_chars * 100 // max(flat_chars, 1)}%）\033[0m")

    # ── 受限行动空间 ─────────────────────────────────────────────
    print("\n\033[1m【2】explorer 的 action space 是主 Agent 的子集\033[0m")
    sub_reg = registry.restricted(SUBAGENT_PRESETS["explorer"].tools)
    print(f"  主 Agent：  {', '.join(registry.names())}")
    print(f"  explorer： {', '.join(sub_reg.names())}")
    blocked = [n for n in registry.names() if n not in sub_reg.names()]
    print(f"  被挡掉的： \033[31m{', '.join(blocked)}\033[0m")
    sub_exec = ToolExecutor(sub_reg, PermissionPolicy(), lambda *a: True, Tracer(False))
    probe = sub_exec.execute("probe", "write", {"path": "x.txt", "content": "hi"}, Session(), 1, 1)
    print(f"\n  就算模型硬猜出 write 这个名字也没用：")
    print(f"    \033[90m{probe.content}\033[0m")
    print("\033[90m  被过滤掉的工具「既不在它的 prompt 里，也拒绝执行」——")
    print("  两者一致才叫限制。只是不告诉它，那不叫限制。\033[0m")

    # ── 父日志里留下了什么 ───────────────────────────────────────
    print("\n\033[1m【3】父日志里关于这次 spawn 的记录\033[0m")
    for ev in session.events():
        if ev.type in (EV_SUBAGENT_START, EV_SUBAGENT_END):
            print(f"  \033[90m#{ev.seq:>3} {ev.type:<16}\033[0m "
                  f"{json.dumps(ev.data, ensure_ascii=False)[:96]}")
    print("\033[90m  过程在子日志里（可单独 replay），父日志只记「派出去了、拿回来了」。\033[0m")

    print("\n\033[90m" + "─" * 68)
    print("Subagent 的价值不是「多调一次 LLM」，而是：")
    print("  1. Context Isolation —— 昂贵的探索过程用完即弃")
    print("  2. 受限 action space —— 子 Agent 在结构上就做不了它不该做的事")
    print("Harness 提供 preset 和 spawn 机制；**派谁去干什么**由模型决定。\033[0m")


def main() -> None:
    global WORKSPACE, SKILLS, INBOX, RT, PROVIDER_FOR_SUBAGENT, PARENT_SESSION, TRACER
    debug = "--debug" in sys.argv
    if "--demo" in sys.argv:
        demo(debug)
        return

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return

    SKILLS = SkillRegistry(Path(__file__).resolve().parent / "skills")
    TRACER = Tracer(enabled=debug)
    log_path = Path(f"session_{uuid.uuid4().hex[:8]}.jsonl")
    session = Session(path=log_path)
    PARENT_SESSION = session
    session.append(EV_SESSION_START, {"cwd": str(WORKSPACE)})
    executor = ToolExecutor(registry, PermissionPolicy(yolo="--yolo" in sys.argv), cli_approver, TRACER)
    INBOX = Inbox()
    RT = RuntimeContext(cwd=WORKSPACE, tool_names=registry.names(),
                        project_notes=load_project_notes(WORKSPACE),
                        skill_catalog=SKILLS.catalog(),
                        subagent_presets=list(SUBAGENT_PRESETS.values()))
    PROVIDER_FOR_SUBAGENT = lambda: provider   # 子 Agent 用同一个模型，但上下文全新

    print("\033[1ms09 — Subagent\033[0m")
    print(f"\033[90m子 Agent 类型：{', '.join(SUBAGENT_PRESETS)}\033[0m")
    print(f"\033[90m日志 {log_path}；/prompt /tree，q 退出\033[0m\n")

    while True:
        try:
            q = input("\033[36m你 > \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("q", "quit", "exit", ""):
            break
        if q == "/tree":
            print_turn_tree(session)
            continue
        if q == "/prompt":
            print_prompt_breakdown(RT, "当前 system prompt 构成")
            continue
        INBOX.put(q)
        try:
            out = run_turn(provider, session, executor, RT, INBOX, TRACER)
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            break
        print(f"\033[32m模型 >\033[0m {out.text}")
        print(f"\033[90m[turn {out.turn} · {out.steps} steps · {out.reason}]\033[0m\n")


if __name__ == "__main__":
    main()
