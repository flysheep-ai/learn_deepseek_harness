#!/usr/bin/env python3
"""s01 — Agent Loop

    User ──▶ LLM ──▶ Assistant
      ▲                  │
      └──────────────────┘

这一章只回答一个问题：**Agent Loop 到底是什么？**

答案先说在前面：它是一个 while 循环，而且这一章的版本还**不是** Agent。
把它跑起来、亲眼看到它做不到什么，是理解 s02 的前提。

运行：
    python s01_agent_loop/code.py --demo     # 离线，不需要 key
    python s01_agent_loop/code.py            # 连真实模型（先配 .env）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

SYSTEM = "你是一个编程助手。回答要简短。"


# ══════════════════════════════════════════════════════════════════
# 循环
# ══════════════════════════════════════════════════════════════════


def conversation_loop(provider) -> None:
    """最小的对话循环。

    这里有一件事值得停下来想清楚，因为后面 17 章都建立在它之上：

        模型是**无状态**的。

    它不记得上一轮说过什么。所谓"它记得"，完全是因为我们每一次请求
    都把**整段历史**重新发了过去。messages 这个 list 就是模型的全部记忆，
    而维护这个 list 的人是我们，不是模型。

    这就是 Harness 的第一个职责，也是最容易被忽略的一个：

        Harness 负责决定「模型下一次请求能看见什么」。

    s05 会发现「用一个 list 当记忆」撑不住，s07 会发现 prompt 本身
    也该是拼装出来的，s10 会发现这个 list 会撑爆 —— 但源头都在这里。
    """
    messages: list[dict] = []

    while True:
        try:
            user_input = input("\033[36m你 > \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if user_input.lower() in ("q", "quit", "exit", ""):
            return

        # 1. 用户输入进入历史
        messages.append({"role": "user", "content": user_input})

        # 2. 把**整段**历史发给模型
        #    注意 system 是单独传的，不在 messages 里 —— 它是每次请求
        #    都重新拼进去的运行时参数，不是对话的一部分。s07 会展开这一点。
        try:
            reply = provider.chat(messages, system=SYSTEM)
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            return

        # 3. 模型的回复也要写回历史，否则下一轮它会忘记自己说过什么
        messages.append(reply.as_assistant_message())

        print(f"\033[32m模型 >\033[0m {reply.text}\n")


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

# 离线演示脚本：让"假模型"表演出这一章要证明的那个局限 ——
# 它知道该干什么，但它只能把命令**说**出来。
DEMO_SCRIPT = [
    scripted(
        "你可以运行 `ls -la` 来查看当前目录的文件。\n"
        "（然后把输出贴给我，我再帮你分析。）"
    ),
    scripted(
        "我没有执行命令的能力，所以看不到 `ls` 的实际输出。\n"
        "麻烦你把结果复制给我。"
    ),
]


def main() -> None:
    demo = "--demo" in sys.argv

    print("\033[1ms01 — Agent Loop\033[0m")
    print("输入问题回车发送，q 退出。\n")

    if demo:
        print("\033[90m[demo] 使用离线假模型，自动喂两个问题\033[0m\n")
        provider = get_provider(demo_script=DEMO_SCRIPT)
        # demo 下不读 stdin，直接把两个问题跑一遍
        messages: list[dict] = []
        for q in ("帮我看看当前目录有哪些文件", "那你自己跑一下呢？"):
            print(f"\033[36m你 > \033[0m{q}")
            messages.append({"role": "user", "content": q})
            reply = provider.chat(messages, system=SYSTEM)
            messages.append(reply.as_assistant_message())
            print(f"\033[32m模型 >\033[0m {reply.text}\n")

        print("\033[90m" + "─" * 62)
        print("循环跑通了，但它不是 Agent：")
        print("  · 模型能说出 `ls -la`，却不能执行它")
        print("  · 命令的输出永远进不了 messages，模型看不见世界的反馈")
        print("  · 中间那个「执行命令、把结果贴回去」的人，是你")
        print("s02 把这个人换成代码。\033[0m")
        return

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return
    print(f"\033[90mprovider={provider.name} model={provider.model}\033[0m\n")
    conversation_loop(provider)


if __name__ == "__main__":
    main()
