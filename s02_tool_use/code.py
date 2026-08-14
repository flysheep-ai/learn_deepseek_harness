#!/usr/bin/env python3
"""s02 — Tool Use

    User ──▶ LLM ──▶ tool_call ──▶ bash ──▶ tool_result ──▶ LLM ──▶ …
                       ▲                                      │
                       └──────────────────────────────────────┘
                              只要模型还在要工具，就继续转

这一章回答：**模型是怎么拥有"行动能力"的？**

s01 的循环里，模型只能把 `ls` 说出来。这一章把"执行命令、把结果贴回去"
的那个人换成代码，于是循环里长出了第二层 —— Agent Loop 真正诞生的地方。

运行：
    python s02_tool_use/code.py --demo
    python s02_tool_use/code.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_llm import LLMError, get_provider, scripted  # noqa: E402

WORKDIR = Path.cwd()
MAX_STEPS = 20  # 防失控：模型可能陷在工具里出不来


# ══════════════════════════════════════════════════════════════════
# s02 新增：工具定义
# ══════════════════════════════════════════════════════════════════

# 这个 dict 就是模型能看到的**全部**行动能力。
#
# 它不是文档，是**契约**：模型只会调用这里描述过的东西。
# 你不写进去，模型就不知道它存在；你写进去了，就必须真的能执行。
#
# 注意它用的是 Harness 自己的中性形状（name / description / parameters），
# 由 harness_llm 里的 provider 翻译成 OpenAI 的 function 或 Anthropic 的
# input_schema。工具定义属于 Harness，不属于某一家模型厂商。
TOOLS = [
    {
        "name": "bash",
        "description": "在工作目录下执行一条 shell 命令，返回 stdout+stderr。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
            },
            "required": ["command"],
        },
    },
]


def run_bash(command: str, cwd: Path) -> str:
    """执行一条命令，把结果变成字符串。

    这里有两个细节值得说明，它们都是"Harness 在保护自己"：

    1. stdout 和 stderr 合并返回。
       模型需要看到报错。把 stderr 丢掉，模型就会以为命令成功了，
       然后基于错误的世界模型继续往下走。**观察必须是诚实的。**

    2. 异常被转成字符串返回，而不是抛出去。
       工具失败是**正常业务**，不是程序崩溃。模型看到
       "command not found" 之后可以自己换一条命令重试；
       但如果异常冒到 Agent Loop 外面，整个会话就没了。

       这条规则后面会被反复用到：**工具的失败要变成模型能读的观察。**
    """
    try:
        r = subprocess.run(
            command, shell=True, cwd=cwd,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            out = f"[exit {r.returncode}]\n{out}"
        return out[:20000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：命令超时（60 秒）"
    except OSError as e:
        return f"错误：{e}"


# ══════════════════════════════════════════════════════════════════
# s02 新增：内层循环 —— 这才是 Agent Loop
# ══════════════════════════════════════════════════════════════════


def agent_loop(provider, messages: list[dict], cwd: Path, system: str) -> str:
    """一次用户输入 → 模型可能连续行动很多步 → 最终给出答复。

    s01 里一次输入 = 一次模型调用。
    s02 里一次输入 = **一次或多次**模型调用，取决于模型自己想动几步。

    这个"多步"就是 Agent 的本质。循环的继续条件只有一个：

        reply.wants_tools          # 也就是 tool_calls 非空

    请注意这句话的分量：**继续与否是模型的输出，不是 Harness 的 if。**
    Harness 从头到尾不知道这是"查文件任务"还是"跑测试任务"，
    它只知道"模型还想要工具就再转一圈"。

    这是全课程的第一条铁律，后面 16 章不会违反它：

        Model decides. Harness enables.
    """
    for step in range(1, MAX_STEPS + 1):
        reply = provider.chat(messages, tools=TOOLS, system=system)

        # 模型说的话要写回历史，**包括它请求了哪些工具**。
        # 少了 tool_calls，下一轮模型就对不上自己的意图和收到的结果。
        messages.append(reply.as_assistant_message())

        # 模型不要工具了 —— 它认为任务结束
        if not reply.wants_tools:
            return reply.text

        for call in reply.tool_calls:
            # s02 只有一个工具，所以直接调。
            # s03 会告诉你这行为什么撑不到第 5 个工具。
            if call.name == "bash":
                print(f"  \033[33m$ {call.arguments.get('command', '')}\033[0m")
                output = run_bash(call.arguments.get("command", ""), cwd)
            else:
                # 模型可能幻觉出不存在的工具名。这不是 bug，是常态。
                # 同样：把它变成一条模型能读懂的观察，而不是崩溃。
                output = f"错误：没有名为 {call.name} 的工具"

            print(f"  \033[90m{output[:200]}\033[0m")

            # ────────────────────────────────────────────────
            # 整个 Harness 里最关键的一次 append。
            #
            # 工具在真实世界里执行完了，但模型**不会自动知道**发生了什么。
            # 环境里的 Observation 必须由 Harness 亲手翻译成
            # 下一次模型请求看得见的消息，否则这一步等于没发生。
            #
            # tool_call_id 必须原样带回：它是 call ↔ result 的唯一配对依据。
            # 少了它、或者配对被切断，模型侧会直接报错。
            # （s10 做上下文压缩时，最难的一点就是不能切断这个配对。）
            # ────────────────────────────────────────────────
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            })

    return f"（达到 {MAX_STEPS} 步上限，停止）"


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════


def make_system(cwd: Path) -> str:
    return (
        f"你是一个编程 Agent，工作目录是 {cwd}。\n"
        "用 bash 工具完成任务。直接动手，不要解释你打算怎么做。"
    )


def build_demo_workspace() -> Path:
    """给离线演示造一个确定的小工程，这样 demo 的输出可复现。"""
    d = Path(tempfile.mkdtemp(prefix="s02_demo_"))
    (d / "hello.py").write_text('print("hello harness")\n', encoding="utf-8")
    (d / "notes.txt").write_text("待办：给 hello.py 加个 main 函数\n", encoding="utf-8")
    return d


DEMO_SCRIPT = [
    scripted(calls=[("bash", {"command": "ls -1"})]),
    scripted(calls=[("bash", {"command": "cat hello.py"})]),
    scripted("目录下有 hello.py 和 notes.txt。hello.py 只有一行 print，还没有 main 函数。"),
]


def main() -> None:
    demo = "--demo" in sys.argv

    print("\033[1ms02 — Tool Use\033[0m")

    if demo:
        cwd = build_demo_workspace()
        provider = get_provider(demo_script=DEMO_SCRIPT)
        print(f"\033[90m[demo] 离线假模型，临时工作目录 {cwd}\033[0m\n")

        question = "这个目录里有什么？hello.py 写了啥？"
        print(f"\033[36m你 > \033[0m{question}")
        messages = [{"role": "user", "content": question}]
        answer = agent_loop(provider, messages, cwd, make_system(cwd))
        print(f"\033[32m模型 >\033[0m {answer}\n")

        print("\033[90m" + "─" * 62)
        print(f"一次用户输入 → {len(provider.seen)} 次模型调用。这就是 Agent Loop。")
        print("最终 messages 的角色序列：")
        print("  " + " → ".join(m["role"] for m in messages))
        print("注意 tool 结果是作为消息**回灌**进上下文的 ——")
        print("模型不会自动知道命令执行了什么，是 Harness 告诉它的。\033[0m")
        return

    try:
        provider = get_provider()
    except LLMError as e:
        print(f"\033[31m{e}\033[0m")
        return

    print(f"\033[90mprovider={provider.name} model={provider.model} cwd={WORKDIR}\033[0m")
    print("\033[31m注意：模型生成的 shell 命令会被真的执行。建议在临时目录里跑。s04 会加权限控制。\033[0m")
    print("输入问题回车发送，q 退出。\n")

    messages: list[dict] = []
    system = make_system(WORKDIR)
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
            answer = agent_loop(provider, messages, WORKDIR, system)
        except LLMError as e:
            print(f"\033[31m{e}\033[0m")
            return
        print(f"\033[32m模型 >\033[0m {answer}\n")


if __name__ == "__main__":
    main()
