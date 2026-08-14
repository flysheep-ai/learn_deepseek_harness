#!/usr/bin/env python3
"""s23 — Self-Extending Harness（自我扩展的 Harness）

    模型操作的不再只是文件系统 —— 而是**自己的运行时**：

      harness_inspect()    观察：我现在由哪些插件组成？
                           有哪些工具、service、事件？

      harness_mount(code)  扩展：写一段代码，作为一个插件
                           挂载进自己的运行时。立即生效。

      harness_unmount(id)  撤回：卸掉它，恢复到我挂载之前。

    这对应 Cordis 论文结论章点名的方向："self-evolving agent
    harnesses，AI 持续生成和替换自己的 harness 组件"。

    这一章解决 dsh 笔记里点名的三个正确性问题：

      1. 模型写的注册必须在**挂载处**当场校验
         —— 坏的 schema 在 mount 时报错，而不是下次请求组装 prompt 时才炸
      2. 模型写的代码要能查到 service 的 API
         —— inspect 提供签名，避免"盲猜方法名"浪费十几步
      3. 模型挂载的一切必须**完全可处置**
         —— 模型按需卸载 + 宿主卸载时顺带清理，否则长会话积累孤儿监听器

    重要边界（与 dsh 一致）：这**不是**沙箱。mount 的代码和 bash
    有同等的信任等级 —— 它跑在共享运行时里。

运行：
    python s23_self_extending/code.py --demo
"""

import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ══════════════════════════════════════════════════════════════════
# 沿用 s13/s14（未改动）：EventBus
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


# ══════════════════════════════════════════════════════════════════
# 沿用 s03/s14（最小版）：Tool / ToolRegistry / Plugin / Harness
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

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _validate_schema(name: str, parameters: dict[str, Any]) -> str | None:
    """模型写的 schema 在这里被**当场**校验。

    这是 dsh 笔记里的正确性问题 #1：坏的 schema 必须在注册处爆炸，
    而不是等下一次请求把它组装进 prompt 时才炸。
    错误信息要可行动 —— 告诉模型怎么改，而不是只说"不对"。
    """
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return f"schema 错误：{name} 的 parameters 必须是 type=object 的 dict"
    props = parameters.get("properties", {})
    if not isinstance(props, dict):
        return f"schema 错误：{name} 的 properties 必须是 dict"
    # 空 properties 合法：无参数工具是常见形态（echo_time 就是）。
    # 校验只抓**结构性错误**，不发明"工具必须有参数"这种假规则。
    required = parameters.get("required", [])
    for r in required:
        if r not in props:
            return f"schema 错误：required 里的 '{r}' 不在 properties 中"
    for pname, pschema in props.items():
        if not isinstance(pschema, dict) or "type" not in pschema:
            return f"schema 错误：property '{pname}' 必须是带 type 的 dict"
    return None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        err = _validate_schema(tool.name, tool.parameters)
        if err:
            raise ValueError(err)      # ← 挂载处校验：问题 #1 的答案
        self._tools[tool.name] = tool
        return lambda: self._tools.pop(tool.name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)


class Plugin:
    name: str = ""

    def setup(self, ctx: "PluginContext") -> None: ...


class PluginContext:
    def __init__(self, harness: "Harness", plugin_name: str) -> None:
        self.harness = harness
        self.plugin_name = plugin_name
        self._disposers: list[Callable[[], None]] = []

    def tool(self, name: str, description: str, parameters: dict[str, Any]) -> Callable:
        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            off = self.harness.tools.register(Tool(name, description, parameters, fn))
            self._disposers.append(off)
            return fn
        return deco

    def on(self, event: str, fn: Callable, order: int = 100) -> Callable:
        off = self.harness.bus.on(event, fn, order, owner=self.plugin_name)
        self._disposers.append(off)
        return fn

    def provide(self, key: str, service: Any) -> None:
        if key in self.harness.services:
            raise ValueError(f"service '{key}' 已被 {self.harness.service_owner[key]} 提供")
        self.harness.services[key] = service
        self.harness.service_owner[key] = self.plugin_name
        self._disposers.append(lambda: (self.harness.services.pop(key, None),
                                        self.harness.service_owner.pop(key, None)))

    def require(self, key: str) -> Any:
        svc = self.harness.services.get(key)
        if svc is None:
            raise RuntimeError(f"插件 {self.plugin_name} 需要 service '{key}'，但没人提供")
        return svc

    def unload(self) -> None:
        for off in reversed(self._disposers):
            off()
        self._disposers.clear()


class Harness:
    """s14 的 Harness 最小版。它仍然没有任何自带功能。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.tools = ToolRegistry()
        self.services: dict[str, Any] = {}
        self.service_owner: dict[str, str] = {}
        self._loaded: dict[str, PluginContext] = {}

    def use(self, plugin: Plugin) -> "Harness":
        if plugin.name in self._loaded:
            raise ValueError(f"插件重复加载：{plugin.name}")
        pctx = PluginContext(self, plugin.name)
        plugin.setup(pctx)
        self._loaded[plugin.name] = pctx
        return self

    def unload(self, name: str) -> None:
        pctx = self._loaded.pop(name, None)
        if pctx:
            pctx.unload()


# ══════════════════════════════════════════════════════════════════
# s23 新增：SelfExtensionPlugin —— inspect / mount / unmount
# ══════════════════════════════════════════════════════════════════

MOUNT_PREFIX = "dyn-"


class SelfExtensionPlugin:
    """三个工具，对应 dsh 的 cordis_inspect / cordis_mount / cordis_unmount。

    设计决策全部照抄 dsh 笔记的取舍：

      · 单 mount 原语，而不是一堆 register_tool / register_listener /
        register_service 结构化工具 —— 一个"挂载插件"覆盖所有能力，
        结构化工具集是永远长不完的 API 面（笔记 Alternatives 表格）。
      · 临时插件只活在进程内存：不写文件、不改配置、不跨会话恢复。
        Model-visible ⟺ logged 仍然成立：mount/unmount 就是
        tool/call + tool/result 对，工具集变化会被 request/header 记录。
      · 这不是沙箱：mount 的代码和 bash 同等信任。
    """

    name = "self-extension"

    def setup(self, ctx: PluginContext) -> None:
        harness = ctx.harness
        mounted: dict[str, Callable[[], None]] = {}    # 挂载 id → 卸载函数

        @ctx.tool("harness_inspect",
                  "查看自己的运行时。不传 what 就全部报告。可选 what=plugins|tools|services|mounted。",
                  {"type": "object",
                   "properties": {"what": {"type": "string",
                                           "description": "plugins | tools | services | mounted"}},
                   "required": []})
        def _inspect(what: str | None = None) -> str:
            """让模型看到自己活在哪台机器里。

            这是正确性问题 #2 的前提：模型写挂载代码之前，
            需要知道 service 的名字和签名 —— 否则每一步都在盲猜。
            （dsh 用生成的 API catalog 保证签名永不过期；教学版直接
            从运行时反射，因为我们的 service 是运行时的唯一真相。）
            """
            parts: list[str] = []
            if what in (None, "plugins"):
                parts.append("插件：" + ", ".join(harness._loaded) or "（无）")
            if what in (None, "tools"):
                parts.append("工具：" + ", ".join(harness.tools.names()) or "（无）")
            if what in (None, "services"):
                svcs = [f"{k}(by {harness.service_owner[k]})" for k in harness.services]
                parts.append("service：" + ", ".join(svcs) or "（无）")
            if what in (None, "mounted"):
                parts.append("临时挂载：" + ", ".join(mounted) or "（无）")
            return "\n".join(parts)

        @ctx.tool("harness_mount",
                  "写一段 Python 代码，作为临时插件挂载进自己的运行时，立即生效。"
                  "代码是一个返回 Plugin 实例的表达式。这是自扩展的核心："
                  "模型可以给自己发明新工具、新监听器、新 service。",
                  {"type": "object",
                   "properties": {
                       "code": {"type": "string",
                                "description": "Python 代码，最后一个表达式是 Plugin 实例"},
                   },
                   "required": ["code"]})
        def _mount(code: str) -> str:
            """挂载 = 执行代码 → 校验 → 进 runtime → 记录可处置性。

            三个正确性问题在这里各有一个回答：

            1. 校验在挂载处：_validate_schema 在 register 时当场爆炸，
               错误信息告诉模型怎么改。
            2. API 可见：模型挂载前可以先 harness_inspect 查 service 名。
            3. 可处置：use() 返回的 disposer 存进 mounted dict；
               宿主插件卸载时（ctx._disposers）也会顺带清空全部临时插件。
            """
            plugin = _eval_plugin(code, harness)
            try:
                harness.use(plugin)
            except Exception as e:  # noqa: BLE001
                return f"挂载失败：{type(e).__name__}: {e}（挂载处校验 —— 什么都没生效）"

            mid = f"{MOUNT_PREFIX}{len(mounted) + 1}"

            def disposer() -> None:
                harness.unload(plugin.name)
                mounted.pop(mid, None)

            mounted[mid] = disposer
            # 宿主插件卸载时，顺带撤掉所有临时插件 —— 可处置性的第二道保险
            ctx._disposers.append(lambda: [d() for d in list(mounted.values())])
            return (f"已挂载临时插件 {mid}（plugin 名 {plugin.name}）。"
                    f"它的工具/监听器立即生效。用完记得 harness_unmount('{mid}')。")

        @ctx.tool("harness_unmount",
                  "卸载一个临时挂载的插件，恢复到挂载之前。只能卸载自己挂的。",
                  {"type": "object", "properties": {"mount_id": {"type": "string"}},
                   "required": ["mount_id"]})
        def _unmount(mount_id: str) -> str:
            disposer = mounted.get(mount_id)
            if disposer is None:
                return f"错误：没有临时挂载 {mount_id}。现有的：{', '.join(mounted) or '（无）'}"
            disposer()
            return f"已卸载 {mount_id}，它的工具/监听器/service 全部消失。"


def _eval_plugin(code: str, harness: Harness) -> Plugin:
    """在受控命名空间里执行模型写的代码。

    注意（与 dsh 一致的边界）：这个命名空间收窄的是**能看到的 API 面**，
    不是**权限**。代码拿到 Plugin 实例后可以注册任何东西，
    包括挂到 pre-execute 上的短路监听器 —— 和 bash 同等的信任等级。

    受控命名空间给三样东西：
      Harness 类（模型用它写 Plugin 子类）
      harness 引用（setup 里可以查 runtime）
      以及 Python 标准库的常规名字（教学版直接 exec，真实版是 vm）
    """
    namespace = {
        "Harness": Harness,
        "Plugin": Plugin,
        "harness": harness,
        "Tool": Tool,
        "field": field,
        "uuid": uuid,
        "time": time,      # 挂载代码里允许的少数内置之一
    }
    # 赋值右侧不能是 class 定义，所以拆成两段：
    # 前面的行用 exec 执行（定义类），最后一行用 eval 求值（实例化）。
    lines = code.strip().split("\n")
    body, last = "\n".join(lines[:-1]), lines[-1]
    exec(body, namespace)
    result = eval(last, namespace)
    if not isinstance(result, Plugin):
        raise ValueError("代码的最后表达式必须是 Plugin 实例")
    return result


# ══════════════════════════════════════════════════════════════════
# 演示
# ══════════════════════════════════════════════════════════════════


def demo() -> None:
    print("\033[1m【1】模型先 inspect：看看自己的运行时\033[0m")
    h = Harness()
    h.use(SelfExtensionPlugin())

    def call_tool(name, **args):
        """教学版的工具执行：直接查注册表。"""
        tool = h.tools.get(name)
        return tool.handler(**args) if tool else f"（没有工具 {name}）"

    print("  " + call_tool("harness_inspect").replace("\n", "\n  "))

    print("\n\033[1m【2】mount：模型给自己发明一个新工具\033[0m")
    code = '''
class TimerPlugin(Plugin):
    name = "my-timer"

    def setup(self, ctx):
        @ctx.tool("echo_time", "返回当前 Unix 时间戳。",
                  {"type": "object", "properties": {}, "required": []})
        def _echo_time():
            import time
            return f"现在的时间戳是 {int(time.time())}"

TimerPlugin()
'''
    print("  \033[90m模型写的代码（节选）：\033[0m class TimerPlugin(Plugin): …echo_time…")
    result = call_tool("harness_mount", code=code)
    print(f"  → \033[32m{result}\033[0m")

    print("\n  mount 立即生效 —— 下一个 step 模型就能调用新工具：")
    print(f"  → echo_time: \033[32m{call_tool('echo_time')}\033[0m")
    print("  再 inspect：")
    print("  " + call_tool("harness_inspect", what="mounted").replace("\n", "\n  "))

    print("\n\033[1m【3】挂载处校验：坏的 schema 当场爆炸\033[0m")
    bad_code = '''
class BadPlugin(Plugin):
    name = "bad-tool"

    def setup(self, ctx):
        @ctx.tool("bad_tool", "坏工具。",
                  {"type": "object", "properties": {}, "required": ["ghost_param"]})
        def _bad():
            return "不会执行到这里"

BadPlugin()
'''
    print(f"  → \033[31m{call_tool('harness_mount', code=bad_code)}\033[0m")
    print("  \033[90m  required 引用了不存在的 property —— 在挂载处被拒，")
    print("  而不是等到下一次请求组装 prompt 时才炸。这就是问题 #1 的答案。\033[0m")

    print("\n\033[1m【4】跨 mount 组合：临时插件之间用 provide/inject 协作\033[0m")
    provider_code = '''
class ClockProvider(Plugin):
    name = "clock-provider"

    def setup(self, ctx):
        ctx.provide("clock", lambda: int(time.time()))

ClockProvider()
'''
    print(f"  → {call_tool('harness_mount', code=provider_code)}")

    consumer_code = '''
class ClockConsumer(Plugin):
    name = "clock-consumer"

    def setup(self, ctx):
        clock = ctx.require("clock")

        @ctx.tool("what_time", "问时钟。",
                  {"type": "object", "properties": {}, "required": []})
        def _what():
            return f"时钟说：{clock()}"

ClockConsumer()
'''
    print(f"  → {call_tool('harness_mount', code=consumer_code)}")
    print(f"  → what_time: \033[32m{call_tool('what_time')}\033[0m")
    print("  \033[90m  挂载之间的依赖关系是普通的 provide/inject —— s20 的机制")
    print("  直接服务于模型自己挂载的组件。dsh 笔记里：卸载 provider 会让")
    print("  consumer 回到 pending，重新 provide 会重新激活它。\033[0m")

    print("\n\033[1m【5】可处置性：unmount 之后什么都不剩\033[0m")
    before = set(h.tools.names())
    print(f"  卸载 my-timer：\033[32m{call_tool('harness_unmount', mount_id='dyn-1')}\033[0m")
    after = set(h.tools.names())
    print(f"  工具集变化：{sorted(before - after)} 消失")
    print(f"  调用它：\033[31m{call_tool('echo_time')}\033[0m")
    print("  \033[90m  问题 #3 的答案：模型按需卸载 + 宿主插件卸载时顺带清理。\033[0m")

    print("\n\033[1m【6】信任边界：这为什么不是沙箱\033[0m")
    print("  mount 的代码可以注册任何东西 —— 包括挂在 pre-execute 上")
    print("  不调 next() 的短路监听器，能停掉 agent 自己的工具派发。")
    print("  所以 mount 和 bash 是同等信任等级：\"有意识地选择信任\"，")
    print("  而不是安全边界。真正的沙箱是另一个问题（s15 的 seam 才是它的家）。\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章的三句话：")
    print("  1. 自扩展 = inspect（观察自己）+ mount（改装自己）+ unmount（撤回）")
    print("  2. 三个正确性问题：挂载处校验 / API 可见 / 完全可处置")
    print("  3. 单 mount 原语优于一堆结构化注册工具；临时插件不持久化")
    print("参考：dsh 笔记 2026-07-08-self-referential-cordis-toolset；Cordis 论文 §8 结论\033[0m")


if __name__ == "__main__":
    demo()
