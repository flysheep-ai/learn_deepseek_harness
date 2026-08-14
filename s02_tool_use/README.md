# s02 — Tool Use

**English version: [README.en.md](README.en.md)**

[s01](../s01_agent_loop/) → **s02** → [s03](../s03_tool_registry/) → … → s18

> 模型是怎么拥有"行动能力"的？
>
> 答案不在模型里，在 Harness 里。

---

## 上一章留下的问题

s01 的模型能说出 `ls -la`，但：

- 它**执行不了**这条命令
- 命令的输出永远进不了 `messages`，模型看不到世界的反馈
- 中间那个"执行命令、把结果贴回去"的人，是你

s01 里一次用户输入 = 一次模型调用。所以模型只有**一次**说话的机会，
它必须在完全看不到环境的情况下一次猜对答案。它在盲猜。

---

## 这一章解决什么

把那个中间人换成代码。换掉之后会发生一件事：**循环长出了第二层。**

```
外层循环（s01 就有）：  用户输入 → … → 用户输入 → …
内层循环（s02 新增）：  模型调用 → 工具 → 模型调用 → 工具 → 模型调用
                        ↑ 一次用户输入之内，模型可以连续走很多步
```

内层这个循环，才是通常说的 **Agent Loop**。

---

## 新增的核心概念

### 1. Tool Schema —— 模型能看见的行动能力

```python
TOOLS = [{
    "name": "bash",
    "description": "在工作目录下执行一条 shell 命令，返回 stdout+stderr。",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]
```

这不是文档，是**契约**：

- 你不写进去，模型就不知道它存在
- 你写进去了，就必须真的能执行

模型的行动空间 = 你交给它的这个 list。一个字都不多。

### 2. tool_call → 执行 → tool_result 回灌

```python
messages.append({
    "role": "tool",
    "tool_call_id": call.id,     # ← 必须原样带回
    "content": output,
})
```

这是整个 Harness 里最关键的一次 `append`。

> 工具在真实世界里执行完了，但**模型不会自动知道发生了什么**。
> 环境里的 Observation 必须由 Harness 亲手翻译成"下一次模型请求看得见的消息"，
> 否则这一步等于没发生。

`tool_call_id` 是 call ↔ result 的唯一配对依据。配对断了模型侧直接报错 ——
s10 做上下文压缩时，最难的一点就是**不能切断这个配对**。

### 3. 循环的继续条件

```python
if not reply.wants_tools:
    return reply.text        # 模型不要工具了 → 它认为任务结束
```

请仔细看这一行的分量：

> **继续与否是模型的输出，不是 Harness 的 if。**

Harness 从头到尾**不知道**这是"查文件任务"还是"跑测试任务"。
它只知道一件事：模型还想要工具，就再转一圈。

这是全课程的第一条铁律：

> **Model decides. Harness enables.**

后面 16 章一次都不会违反它。当你在 s16 看到多 Agent 协作、
在 s17 看到目标循环时，回头对照这一行。

---

## 最小架构图

```
  用户输入
     │
     ▼
  ┌────────────────────── agent_loop ──────────────────────┐
  │                                                        │
  │   messages ──▶ LLM ──▶ Reply                           │
  │      ▲                   │                             │
  │      │                   ├── 无 tool_calls ──▶ 返回文本 │
  │      │                   │                             │
  │      │                   └── 有 tool_calls              │
  │      │                          │                       │
  │      │                          ▼                       │
  │      │                     run_bash()                   │
  │      │                          │                       │
  │      └──── role:"tool" 消息 ◀────┘                       │
  │            （Observation 回灌）                          │
  └────────────────────────────────────────────────────────┘
```

---

## 跑一下

```sh
python s02_tool_use/code.py --demo
```

输出：

```
你 > 这个目录里有什么？hello.py 写了啥？
  $ ls -1
  hello.py
  notes.txt
  $ cat hello.py
  print("hello harness")
模型 > 目录下有 hello.py 和 notes.txt。hello.py 只有一行 print，还没有 main 函数。

一次用户输入 → 3 次模型调用。这就是 Agent Loop。
最终 messages 的角色序列：
  user → assistant → tool → assistant → tool → assistant
```

**盯住那个角色序列。** 一次用户输入，产生了 3 次模型调用和 2 次工具执行。
s01 永远只能是 `user → assistant`。

---

## 执行流程

```
messages = [user]
  ↓
① chat(messages, tools=TOOLS)
   → Reply(tool_calls=[bash("ls -1")])
   → messages += [assistant(tool_calls=…)]
   → run_bash("ls -1") → "hello.py\nnotes.txt"
   → messages += [tool(result)]
  ↓
② chat(messages, tools=TOOLS)                 ← 模型这次看得见 ls 的输出了
   → Reply(tool_calls=[bash("cat hello.py")])
   → messages += [assistant, tool]
  ↓
③ chat(messages, tools=TOOLS)
   → Reply(text="目录下有…", tool_calls=())    ← 不要工具了
   → 退出循环，返回文本
```

---

## 为什么这样设计

### 为什么工具失败不抛异常

```python
except OSError as e:
    return f"错误：{e}"          # ← 返回字符串，不 raise
```

**工具失败是正常业务，不是程序崩溃。**

模型看到 `command not found` 之后，完全可以自己换一条命令重试 ——
这正是我们想要的自主性。但如果异常冒到 `agent_loop` 外面，整个会话就没了。

这条规则在后面反复出现：

> **工具的失败要变成模型能读的观察。**

同理，模型幻觉出不存在的工具名时，我们也返回
`错误：没有名为 xxx 的工具`，而不是 `KeyError`。幻觉工具名是常态，不是 bug。

### 为什么 stderr 要和 stdout 一起返回

把 stderr 丢掉，模型会以为命令成功了，然后基于一个错误的世界模型继续往下走。

> **观察必须是诚实的。**

Harness 可以限制模型能做什么（s04），但不应该欺骗模型发生了什么。

### 为什么有 MAX_STEPS

```python
for step in range(1, MAX_STEPS + 1):
```

模型可能陷在工具里出不来（反复读同一个文件、反复试同一条失败命令）。

注意这个上限的性质：它是**资源保护**，不是**智能判断**。我们没有写
"如果连续 3 次失败就换策略" —— 那会是 Harness 替模型思考。
我们只是说"你最多花这么多步"，怎么用是模型的事。

s17 会把这种约束正式化成 Goal 的预算。

---

## 与上一章相比发生了什么

| | s01 | s02 |
|---|---|---|
| 模型调用/输入 | 恰好 1 次 | 1 到 N 次，**N 由模型决定** |
| 工具 | 无 | `bash` |
| messages 里的角色 | user / assistant | user / assistant / **tool** |
| 循环层数 | 1（对话） | 2（对话 + **step**） |
| 模型能否看见环境 | 不能 | **能**（通过 tool_result 回灌） |

代码上的具体差异：

```python
# s01
reply = provider.chat(messages, system=SYSTEM)
messages.append(reply.as_assistant_message())

# s02
reply = provider.chat(messages, tools=TOOLS, system=system)   # ← 多传了 tools
messages.append(reply.as_assistant_message())
if not reply.wants_tools:                                     # ← 新的终止条件
    return reply.text
for call in reply.tool_calls:                                 # ← 执行 + 回灌
    output = run_bash(call.arguments["command"], cwd)
    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
```

---

## 真实系统里还有什么

- **并行工具执行**：模型一次可以请求多个 tool_call。我们串行执行。
  DeepSeek Harness 会用 `isConcurrencySafe` 分类，把安全的放进并发池，
  不安全的设 barrier。这很真实，但会淹没"pre/execute/post"这个形状本身，
  所以本项目全程串行。
- **超时与取消**：真实系统给每个工具一个 `timeoutMs`，并把 abort signal
  一路传进工具体内。我们只给 `subprocess` 一个 60 秒硬超时。
- **输出截断策略**：我们粗暴地 `[:20000]`。真实系统会保留头尾、
  提示"中间省略 N 行"，因为模型往往需要看结尾的报错。

---

## 自己动手改

1. **加一个 `get_current_time` 工具**
   往 `TOOLS` 里加一条 schema，在执行分支里加一个 `elif`。
   加完之后数一下：你改了**几处**？（schema 一处、dispatch 一处）
   记住这个数字，s03 就是冲着它来的。

2. **把 stderr 丢掉**
   把 `(r.stdout + r.stderr)` 改成 `r.stdout`，然后让模型
   `cat 一个不存在的文件`。观察它怎么被骗到 —— 它会以为文件是空的。

3. **把 tool 结果的回灌删掉**
   注释掉 `messages.append({"role": "tool", ...})`。
   （模型侧会直接报错，因为有 tool_call 没有配对的 result。
   这个报错本身就说明了配对的强制性。）

4. **看看模型到底收到了什么**
   在 demo 里加 `print(provider.seen[1]["messages"])`，
   看第 2 次请求时上下文长什么样。

---

## 下一章

试着做完"自己动手改"的第 1 题，然后想象再加 4 个工具：`read` / `write` / `edit` / `glob`。

```python
if call.name == "bash":
    ...
elif call.name == "read":
    ...
elif call.name == "write":
    ...
```

这个 `elif` 链有三个问题，而且会越来越疼：

1. schema 写在 `TOOLS` 里，实现写在 `elif` 里 —— 两处会**对不上**（改了参数名忘了改另一边）
2. 参数怎么传？`run_read(**call.arguments)`？模型少传一个必填参数就是 `TypeError`
3. 想给所有工具统一加一层"执行前检查"，你得在 5 个分支里各写一遍

工具多起来之后，Harness 到底需要什么结构？

→ [s03 — Tool Registry](../s03_tool_registry/)
