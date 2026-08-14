"""harness_llm.py — 本项目**唯一**的共享模块。

它里面没有任何 Harness 逻辑：
没有 agent loop、没有 tool registry、没有 session、没有 permission。
只有「把 messages 发给某个模型，拿回归一化的回复」这一件事。

之所以允许它被 18 个章节共享，是因为 HTTP 传输不是这门课要教的东西。
每一章要教的 Harness 机制，都完整地写在那一章自己的 code.py 里。

------------------------------------------------------------------
提供三个 provider
------------------------------------------------------------------

    OpenAICompatProvider   OpenAI 兼容的 /chat/completions（DeepSeek、Kimi、
                           vLLM、Ollama、OpenAI 本身……都是这个形状）
    AnthropicProvider      直连 /v1/messages（不依赖 anthropic SDK）
    ScriptedProvider       离线假模型：按预先写好的脚本逐条返回

ScriptedProvider 是这个项目能「每章都实际跑起来」的原因。
它让读者在没有 API key 的时候就能 `python code.py --demo` 看到完整流程，
也让 tests/ 可以对 Harness 行为做确定性断言 —— 真实模型是不确定的，
但 Harness 的行为必须是确定的，这两者要能分开测。

------------------------------------------------------------------
两个归一化约定（贯穿全部 18 章）
------------------------------------------------------------------

1. messages 用 OpenAI 的裸 dict 形状，不包装成自定义类型：

       {"role": "system",    "content": "..."}
       {"role": "user",      "content": "..."}
       {"role": "assistant", "content": "...", "tool_calls": [...]}
       {"role": "tool",      "tool_call_id": "...", "content": "..."}

   读者已经认识这个形状。换成自定义 Message 类，只会让
   「Harness 到底做了什么」被类型转换的噪音掩盖。

2. tool schema 用 Harness 自己的中性形状，由 provider 负责翻译：

       {"name": ..., "description": ..., "parameters": {...JSON Schema...}}

   这一点本身就是一个 Harness 观念：工具定义属于 Harness，
   不属于任何一家模型厂商的 wire format。s15 会把这件事正式称为 seam。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

__all__ = [
    "ToolCall",
    "Reply",
    "LLMProvider",
    "LLMError",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "ScriptedProvider",
    "scripted",
    "get_provider",
    "load_dotenv",
]


# ══════════════════════════════════════════════════════════════════
# 归一化的返回类型
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolCall:
    """模型请求执行一次工具。

    id 由模型（或 provider）生成，Harness 必须原样带回 tool 结果里。
    这个 id 是「call ↔ result」配对的唯一依据 —— s05 讲事件日志、
    s10 讲上下文压缩时，都要靠它保证配对不被切断。
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """一次模型请求的归一化结果。

    provider 的职责到此为止：把各家 API 的形状抹平成这四个字段。
    再往上（要不要执行工具、结果怎么回灌）是 Harness 的事。
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        """模型是否还想继续行动。

        Agent Loop 的唯一继续条件。注意它是**模型的输出**，
        不是 Harness 里的某个 if —— 这是全课程的第一条铁律。
        """
        return bool(self.tool_calls)

    def as_assistant_message(self) -> dict[str, Any]:
        """把回复转回 messages 里的 assistant 条目。

        必须原样带上 tool_calls：下一次请求时模型要看到自己上一步
        请求了什么，否则它无法把 tool 结果和自己的意图对上。
        """
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                }
                for c in self.tool_calls
            ]
        return msg


class LLMError(RuntimeError):
    """模型请求失败。Harness 需要能把它和「工具执行失败」区分开。"""


class LLMProvider(Protocol):
    """模型访问的接口。

    整个项目里，Harness 只认识这一个方法。
    换模型厂商 = 换一个实现了它的对象，Agent Loop 一行不改。
    """

    name: str
    model: str

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> Reply: ...


# ══════════════════════════════════════════════════════════════════
# OpenAI 兼容
# ══════════════════════════════════════════════════════════════════


class OpenAICompatProvider:
    """OpenAI 兼容的 /chat/completions。

    DeepSeek / Kimi / Qwen / vLLM / Ollama / OpenAI 都走这条路径。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.name = "openai-compat"
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def chat(self, messages, tools=None, system=None) -> Reply:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
            "max_tokens": self.max_tokens,
        }
        if tools:
            # Harness 的中性 schema → OpenAI 的 function 包装。
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                }}
                for t in tools
            ]

        try:
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001 - 统一成 LLMError 交给上层决定重试
            raise LLMError(f"{self.name} request failed: {e}") from e

        choice = data["choices"][0]["message"]
        calls = []
        for c in choice.get("tool_calls") or []:
            fn = c["function"]
            # arguments 是模型生成的字符串，可能不是合法 JSON。
            # 这里不抛异常：让它变成一次「参数错误」的工具结果，
            # 模型看到错误信息后自己重试，比让整个 loop 崩掉更好。
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__malformed_arguments__": fn.get("arguments", "")}
            calls.append(ToolCall(id=c.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=fn["name"], arguments=args))

        return Reply(
            text=choice.get("content") or "",
            tool_calls=tuple(calls),
            usage={
                "input": data.get("usage", {}).get("prompt_tokens", 0),
                "output": data.get("usage", {}).get("completion_tokens", 0),
            },
            raw=data,
        )


# ══════════════════════════════════════════════════════════════════
# Anthropic
# ══════════════════════════════════════════════════════════════════


class AnthropicProvider:
    """Anthropic /v1/messages，直接用 httpx，不装 SDK。

    留着它是为了证明一件事：Harness 侧的 messages 形状不必跟着厂商走。
    两家 API 的差异（system 独立字段、content blocks、tool_result 必须
    合并进 user 消息）全部被压在这个类里面。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    @staticmethod
    def _to_anthropic(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI 形状 → Anthropic content blocks。

        最容易踩的一点：OpenAI 里每个 tool 结果是**一条独立消息**，
        Anthropic 里它们必须作为 tool_result block 合并进**一条 user 消息**。
        所以这里要把连续的 tool 消息攒起来一次性 flush。
        """
        out: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []

        def flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            role = m.get("role")
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": str(m.get("content", "")),
                })
                continue
            flush()
            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    fn = c["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({"type": "tool_use", "id": c["id"], "name": fn["name"], "input": args})
                # Anthropic 拒绝空 content
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
            elif role == "system":
                # system 在 Anthropic 是顶层字段，混在 messages 里会被拒绝，
                # 这里降级成一条 user 消息而不是丢弃。
                out.append({"role": "user", "content": [{"type": "text", "text": m["content"]}]})
            else:
                out.append({"role": "user", "content": [{"type": "text", "text": str(m.get("content", ""))}]})

        flush()
        return out

    def chat(self, messages, tools=None, system=None) -> Reply:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_anthropic(messages),
            "max_tokens": self.max_tokens,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]

        try:
            r = httpx.post(
                f"{self.base_url}/v1/messages",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{self.name} request failed: {e}") from e

        text_parts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}))

        return Reply(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            usage={
                "input": data.get("usage", {}).get("input_tokens", 0),
                "output": data.get("usage", {}).get("output_tokens", 0),
            },
            raw=data,
        )


# ══════════════════════════════════════════════════════════════════
# 离线假模型
# ══════════════════════════════════════════════════════════════════


def scripted(text: str = "", calls: Sequence[tuple[str, dict[str, Any]]] = ()) -> Reply:
    """写脚本用的快捷构造。

        scripted(calls=[("bash", {"command": "ls"})])
        scripted("做完了")
    """
    return Reply(
        text=text,
        tool_calls=tuple(
            ToolCall(id=f"call_{i}_{uuid.uuid4().hex[:6]}", name=n, arguments=a) for i, (n, a) in enumerate(calls)
        ),
    )


class ScriptedProvider:
    """按脚本逐条返回的假模型。

    每次 chat() 消费脚本里的下一项。脚本项可以是：
      - Reply                      直接返回
      - callable(messages) -> Reply  想根据上下文分支时用

    脚本用完之后返回一句收尾文本，这样 Agent Loop 一定会正常终止，
    不会因为脚本写短了而空转。

    它同时是一个观察点：self.seen 记录了每次请求实际收到的
    (messages, tools, system)。s07 讲 prompt assembly、s09 讲
    context isolation、s10 讲 compaction 时，测试就是靠断言
    「模型到底看见了什么」来验证 Harness 有没有做对。
    """

    def __init__(self, script: Sequence[Reply | Callable[[list[dict[str, Any]]], Reply]] = (), tail: str = "完成。"):
        self.name = "scripted"
        self.model = "scripted-demo"
        self.script = list(script)
        self.tail = tail
        self.cursor = 0
        self.seen: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, system=None) -> Reply:
        self.seen.append({
            "messages": [dict(m) for m in messages],
            "tools": [t["name"] for t in (tools or [])],
            "system": system,
        })
        # 粗略的 token 估算（约 4 字符 1 token）。
        # 假模型也要报 usage：s05 的 request/usage 事件、s10 的上下文压力判断
        # 都依赖它。离线演示如果没有 usage，那两章就没东西可看了。
        approx_in = sum(len(str(m.get("content", ""))) for m in messages) // 4 + len(str(system or "")) // 4

        if self.cursor >= len(self.script):
            return Reply(text=self.tail, usage={"input": approx_in, "output": len(self.tail) // 4})
        item = self.script[self.cursor]
        self.cursor += 1
        reply = item(list(messages)) if callable(item) else item
        if not reply.usage:
            out = len(reply.text) // 4 + sum(len(json.dumps(c.arguments)) for c in reply.tool_calls) // 4
            reply = Reply(text=reply.text, tool_calls=reply.tool_calls,
                          usage={"input": approx_in, "output": out}, raw=reply.raw)
        return reply


# ══════════════════════════════════════════════════════════════════
# 环境装配
# ══════════════════════════════════════════════════════════════════


def load_dotenv(path: str | Path = ".env") -> None:
    """极小的 .env 读取，避免为此引入 python-dotenv。

    已存在的环境变量优先，不覆盖。
    """
    p = Path(path)
    if not p.exists():
        # 也在本文件所在目录找一次，方便从任意章节目录启动
        p = Path(__file__).resolve().parent / ".env"
        if not p.exists():
            return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def get_provider(demo_script: Sequence[Any] | None = None) -> LLMProvider:
    """按环境变量装配 provider。

        LLM_PROVIDER   openai | anthropic | scripted   （不填则自动推断）
        LLM_BASE_URL   OpenAI 兼容端点，默认 https://api.openai.com/v1
        LLM_API_KEY    密钥
        LLM_MODEL      模型 id

    传了 demo_script 就强制走离线假模型 —— 这是每章 `--demo` 的入口。
    """
    if demo_script is not None:
        return ScriptedProvider(demo_script)

    load_dotenv()
    kind = os.getenv("LLM_PROVIDER", "").strip().lower()
    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
    base = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
    model = os.getenv("LLM_MODEL") or ""

    if not kind:
        kind = "anthropic" if (os.getenv("ANTHROPIC_API_KEY") and not os.getenv("LLM_API_KEY")) else "openai"

    if kind == "scripted" or not key:
        raise LLMError(
            "没有配置模型。两条路：\n"
            "  1) 复制 .env.example 为 .env，填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL\n"
            "  2) 直接加 --demo 用离线假模型跑一遍（不需要任何 key）"
        )

    if kind == "anthropic":
        return AnthropicProvider(
            model=model or "claude-sonnet-4-5",
            api_key=key,
            base_url=base or "https://api.anthropic.com",
        )
    return OpenAICompatProvider(
        model=model or "gpt-4o-mini",
        api_key=key,
        base_url=base or "https://api.openai.com/v1",
    )
