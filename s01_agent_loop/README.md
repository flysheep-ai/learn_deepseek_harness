# s01 — Agent Loop

**English version: [README.en.md](README.en.md)**

**s01** → [s02](../s02_tool_use/) → s03 → … → s18

> 这一章的代码**不是** Agent。
> 把它跑起来，看清楚它做不到什么 —— 这是理解后面 17 章的前提。

---

## 这一章解决什么问题

你想让模型帮你干活：

> "帮我看看当前目录有哪些文件。"

模型回你：

> "你可以运行 `ls -la`。（然后把输出贴给我。）"

于是你切到终端，敲 `ls -la`，复制输出，粘回对话框。模型看完说"接下来运行 `pytest`"，你再敲一遍、再粘一遍。

**每一个来回，你都在给模型当中间层。** 你在做三件事：

1. 把模型说的命令**执行**掉
2. 把执行结果**送回**模型的上下文
3. 判断**要不要继续**下一轮

这三件事就是 Harness 的全部工作。这一章先把最外层的框架搭出来。

---

## 新增的核心概念

### 概念一：Loop

```python
while True:
    user_input = input()
    messages.append({"role": "user", "content": user_input})
    reply = provider.chat(messages, system=SYSTEM)
    messages.append(reply.as_assistant_message())
    print(reply.text)
```

五行。没有类，没有抽象，没有注册表。这一章不需要。

### 概念二：模型是无状态的

这一点比循环本身重要得多。

模型**不记得**上一轮说过什么。所谓"它记得"，完全是因为我们每次请求都把整段 `messages` 重新发过去。

```
第 1 次请求发送：[user1]
第 2 次请求发送：[user1, assistant1, user2]          ← user1 又发了一遍
第 3 次请求发送：[user1, assistant1, user2, assistant2, user3]
```

所以：

> **`messages` 这个 list 就是模型的全部记忆，而维护它的人是 Harness，不是模型。**

这是 Harness 的第一个职责，也是最容易被忽略的一个：

> **Harness 决定模型下一次请求能看见什么。**

后面每一章几乎都在回答这个问题的一个变体：
工具结果怎么进去（s02）、system prompt 怎么拼进去（s07）、
技能文档什么时候进去（s08）、装不下了怎么办（s10）、
子 Agent 的垃圾怎么**不**进去（s09）。

---

## 最小架构图

```
   ┌──────┐   messages   ┌──────┐
   │ User │ ───────────▶ │ LLM  │
   └──────┘              └──────┘
       ▲                     │
       │       text          │
       └─────────────────────┘

   Harness 在这里只做了一件事：维护 messages
```

---

## 执行流程

```
你输入      "帮我看看当前目录有哪些文件"
  ↓
messages    [{"role":"user", "content":"帮我看看..."}]
  ↓
provider.chat(messages, system=SYSTEM)
  ↓
Reply(text="你可以运行 `ls -la`……", tool_calls=())
  ↓
messages    [..., {"role":"assistant", "content":"你可以运行..."}]
  ↓
打印，回到第一步
```

---

## 跑一下

```sh
python s01_agent_loop/code.py --demo    # 离线假模型，不需要任何 key
python s01_agent_loop/code.py           # 真实模型（先 cp .env.example .env）
```

`--demo` 用的是 `harness_llm.ScriptedProvider` —— 一个按脚本返回的假模型。
整个项目 18 章都支持 `--demo`，这样你可以先把机制看明白，再决定要不要接真模型。

---

## 为什么这样设计

### 为什么 s01 不写 `class Agent`

因为现在还不需要。

这个项目最重要的一条规则是：**抽象必须由痛点触发，不能提前发明。**

如果 s01 就给你 `Agent` / `AgentConfig` / `AgentContext` 三个类，你会以为它们是"标准答案"，
但你其实不知道它们为什么存在。而当你在 s13 亲眼看到"给工具加个耗时统计要改 Executor、加 sandbox 又要改 Executor"之后，
再引入 EventBus，你会觉得它是**必然的**，不是被规定的。

所以每一章都遵守：

> Simple first. Abstraction later.
> 先让结构坏掉，再修它。

### 为什么 system prompt 单独传，不塞进 messages

```python
provider.chat(messages, system=SYSTEM)   # ← system 是参数，不是消息
```

因为 system prompt 不是"对话的一部分"，它是**每次请求重新拼装出来的运行时参数**。

现在它是一个常量字符串，看不出区别。但到 s07 你会看到它变成：
基础指令 + 当前工具清单 + 工作目录 + 已加载技能 + 任务状态 —— 每一步都可能不一样。

把它和对话历史混在一起，s07 就没法拆了。

---

## 与上一章相比发生了什么

这是第一章，没有上一章。但和"直接调 API"相比：

| | 直接调 API | s01 |
|---|---|---|
| 记忆 | 每次请求都是独立的 | `messages` 累积历史 |
| 循环 | 一问一答 | `while True` |
| 行动能力 | 无 | **仍然是无** |

---

## 真实系统里还有什么

我们这里省掉的（后面章节或工业实现会补）：

- **流式输出**：真实 Harness 一边收 token 一边渲染，DeepSeek Harness 甚至把每个 chunk 都写进日志（`assistant/chunk`）用于回放。本项目全程非流式，因为它和每一章的主概念正交。
- **重试与错误恢复**：模型请求会超时、会 429、会返回超长上下文错误。我们只把它包成 `LLMError`。
- **token 计量**：`Reply.usage` 已经带回来了，但 s01 还没有人用它。s10 会用。

---

## 自己动手改

1. **看见模型的记忆**
   在 `provider.chat` 之前加一行 `print(len(messages), messages)`，跑三轮，
   确认第 3 次请求确实把第 1 轮的内容又发了一遍。

2. **把记忆砍掉**
   把 `provider.chat(messages, ...)` 改成 `provider.chat(messages[-1:], ...)`，
   然后问"我刚才问了什么？"。观察模型是怎么"失忆"的 ——
   这能让你确信记忆真的在 Harness 手里。

3. **让假模型说别的**
   改 `DEMO_SCRIPT` 里的文本，重跑 `--demo`。
   顺便看一眼 `harness_llm.ScriptedProvider`：它把每次请求收到的
   `messages / tools / system` 都记在 `self.seen` 里 ——
   后面几章的测试全靠断言"模型到底看见了什么"。

---

## 下一章

现在模型只能**说**出 `ls -la`，不能执行它。命令的输出永远进不了 `messages`，
所以模型永远看不到世界的反馈 —— 它在盲猜。

那个"执行命令、把结果贴回去"的人，能不能换成代码？

换掉之后，循环该在什么时候停？模型说完话就停吗，还是它想继续动手时要接着转？

→ [s02 — Tool Use](../s02_tool_use/)
