# s05 — Session Event Log

**English version: [README.en.md](README.en.md)**

[s04](../s04_permission/) → **s05** → [s06](../s06_turn_and_step/) → … → s18

> 这一章推翻了前四章的一个隐含假设。
>
> **`messages` 不是真相，它是真相的一个投影。**

---

## 上一章留下的问题

s04 结束时，我们有工具、有权限、有审计流水。但所有东西还是塞在一个 list 里：

```python
messages: list[dict] = []
executor.audit: list[dict] = []
```

三个问题正在逼近：

**1. 进程一挂，会话全没了。** 没有任何持久化。

**2. 审计流水和 messages 对不上号。**
审计说"第 4 次调用被拒了"，但那是 `messages` 里第几条？没人知道。
两个 list，两套编号，永远对不齐。

**3. 有些东西必须记录，但不该给模型看。**

| 事实 | 塞进 messages？ | 丢掉？ |
|---|---|---|
| 用户在第 3 步点了"拒绝" | 污染上下文 | 审计就瞎了 |
| 这次请求烧了 4200 token | 纯属浪费 | 没法做预算 |
| 工具在执行前就登记了 | 模型不需要 | 崩溃时无法定位 |

**这些事实无处安放。**

而更深的问题是：如果 `messages` 是真相，那么
"恢复会话"、"回放执行"、"看看第 3 步时模型看见了什么"、"从中间分叉出一个新会话"
—— 这些事**根本无从做起**，因为中间状态早被覆盖了。

---

## 这一章解决什么

把真相搬个家：

```
      ❌ 之前                        ✅ 现在

   messages（真相）              事件日志（append-only，真相）
        │                              │
        └─▶ 发给模型                   │  derive_messages()
                                       ▼
                                  messages（投影，用完即弃）
```

一句话：

> **Model-visible means logged.**
> 凡是能进入模型请求的东西，都必须能从日志重建出来。

---

## 新增的核心概念

### 1. SessionEvent：一条不可变的事实

```python
@dataclass(frozen=True)
class SessionEvent:
    seq: int          # 单调递增且连续 —— 事件的身份
    type: str         # "user/message" / "tool/call" / ...
    data: dict        # 必须是纯 JSON
    time: float
```

三个性质缺一不可：

- **只追加**：写下去就不改。允许改历史，回放/fork/审计就全部失效。
- **有序号**：`seq` 是事件的身份，也是"上下文压缩"能精确指定范围的前提（s10 会用）。
- **可序列化**：存不下来的事件等于没记。所以 `append()` 里当场 `json.dumps` 检查——
  一个存不下来的事件必须**立刻**炸，而不是等崩溃恢复时才发现日志早就残缺了。

### 2. SURFACE vs log-only：本章最需要记住的划分

```python
SURFACE_EVENTS = {"user/message", "assistant/message", "tool/result"}

# log-only：session/start, tool/call, permission/decision, request/usage
```

只有 SURFACE 事件参与投影，其余留在日志里。

> Model-visible means logged。
> **但反过来不成立** —— 日志里有大量东西刻意不给模型看。

这一点是很多人第一次读工业 Harness 时的困惑来源。这里把它写成两个显式的集合，就不会混。

### 3. 为什么 `tool/call` 是 log-only，却还要单独记一条

模型请求了哪些工具，这个信息已经在 `assistant/message` 里了（assistant 消息本身带 `tool_calls`），
再投影一次就重复。所以它不是 SURFACE。

但它**必须单独记，而且必须在执行之前记**：

```python
session.append(EV_TOOL_CALL, {...})   # ← 先登记
short = self.pre_execute(ctx, session)
result = ... run_body(ctx)            # ← 再执行（这里可能崩）
session.append(EV_TOOL_RESULT, {...})
```

一条 `tool/call` 后面没有配对的 `tool/result`，就是"执行到一半崩了"的铁证。

如果反过来（执行完再记一条），崩溃现场看起来像**这次调用从未发生** ——
那是最危险的一种日志：它在说谎。

### 4. derive_messages()：一个纯投影

```python
def derive_messages(session, upto=None) -> list[dict]:
    messages = []
    for ev in session.events(upto):
        if ev.type not in SURFACE_EVENTS:
            continue
        ...
    return messages
```

同样的事件流永远得到同样的 messages，没有任何隐藏状态。

`upto` 参数是白送的红利：**任意历史时刻的上下文都能精确重算。**

---

## 最小架构图

```
   用户输入 ──┐
              ▼
   ┌──────────────────────────────────────┐
   │        Session（append-only）         │
   │  #1 session/start        log-only    │
   │  #2 user/message         SURFACE     │
   │  #3 assistant/message    SURFACE     │
   │  #4 request/usage        log-only    │
   │  #5 tool/call            log-only    │
   │  #6 permission/decision  log-only    │
   │  #7 tool/result          SURFACE     │
   │  …                                   │
   └───────────┬──────────────────────────┘
               │
               ├──▶ session.jsonl（磁盘，可恢复）
               │
               ▼  derive_messages()  ← 只取 SURFACE
        ┌──────────────┐
        │   messages   │  用完即弃，每步重算
        └──────┬───────┘
               ▼
              LLM
```

---

## 跑一下

```sh
python s05_session_event_log/code.py --demo
```

demo 分三段，每段证明一件前四章做不到的事。

### 第一段：19 条事件 → 8 条消息

```
# 1 log-only session/start
# 2 SURFACE  user/message         把 app.py 版本号升到 0.2.0，然后清理一下环境
# 3 SURFACE  assistant/message    (请求 1 个工具)
# 4 log-only request/usage        in=41 out=4
# 5 log-only tool/call            read {"path": "app.py"}
# 6 log-only permission/decision  read: allow (只读操作)
# 7 SURFACE  tool/result              1  VERSION = "0.1.0"
…
#16 log-only permission/decision  bash: deny (递归删除根目录或家目录)
#17 SURFACE  tool/result          权限拒绝：递归删除根目录或家目录…
```

**11 条 log-only 事件没有进上下文**，但它们全都在磁盘上。
"模型试图 `rm -rf ~` 被拦了"这条事实，和模型看到的那条拒绝文字，
现在处在**同一条时间线**上（#16 和 #17）。s04 做不到这件事。

### 第二段：杀进程，从磁盘恢复

```python
del session
restored = Session.load(log_path)     # 读回 19 条事件
```

然后问模型"版本号是多少？是你改的吗？"，它答得出来 ——
因为恢复后的上下文和崩溃前**逐条一致**。它不是被存下来的，是被**重新算出来**的。

### 第三段：时间旅行

```python
derive_messages(restored, upto=5)     # 第 5 号事件时，模型看见了什么？
→ user / assistant  （只有 2 条）
```

---

## 执行流程

```
用户输入
   │
   ├─▶ session.append("user/message", …)
   │
   ▼
agent_loop
   │
   ├─ messages = derive_messages(session)      ← 每一步都重新投影
   ├─ reply = provider.chat(messages, …)
   ├─▶ session.append("assistant/message", …)
   ├─▶ session.append("request/usage", …)      ← log-only
   │
   └─ 对每个 tool_call：
        ├─▶ session.append("tool/call", …)     ← 执行前登记，log-only
        ├─▶ session.append("permission/…", …)  ← log-only
        ├─  执行
        └─▶ session.append("tool/result", …)   ← SURFACE
```

---

## 为什么这样设计

### 为什么每一步都重新 derive，不缓存

```python
for _ in range(MAX_STEPS):
    messages = derive_messages(session)     # 每步重算
```

这不是性能最优的写法。但它保证了一个不变量：

> **模型看到的东西，永远等于日志能重建出来的东西。**

一旦你为了省事在旁边缓存一份 `messages`，这个不变量就会悄悄破掉 ——
某天某处直接往缓存里 append 了一条，日志里却没有，
于是"恢复后行为不一致"，而且极难查。

真实系统当然会加缓存，但那是**带失效机制的派生缓存**，不是第二份真相。

### 为什么 agent_loop 的参数变了（全课程唯一一次）

```python
def agent_loop(provider, messages: list, ...)     # s04
def agent_loop(provider, session: Session, ...)   # s05
```

s03/s04 我一直在强调"loop 一行没改"。这一章改了，值得说明为什么值得。

s04 的 loop **持有真相**。于是：

- 想持久化 → 得序列化 loop 的内部状态
- 想回放 → 没有任何切入点
- 想知道"第 3 步模型看见了什么" → 那个中间状态早被覆盖了

现在 loop 只负责**追加事实**，上下文在每次请求前重新算出来。

> loop 从"状态持有者"降级成了"事实生产者"。
> 它变简单了，能力反而变强了。

这是本课程里少数几次"抽象让代码变少"的时刻。

### 为什么 `Session` 里没有 `messages` 字段

这是刻意的。有那个字段，就一定会有人往里写，然后就有了第二份真相。

**不给它存在的机会。**

### 为什么把权限决定记成 log-only 而不是塞进消息

模型不需要知道权限系统的内部形态（`decision: "ask"`, `approved: false`），
它只需要看到那条人话结果："用户拒绝了这次操作，请换一种方式。"

**同一件事实，对不同受众有不同的表示。**
日志存完整形态，投影只给模型看它需要的部分 —— 这正是"投影"这个词的含义。

---

## 与上一章相比发生了什么

| | s04 | s05 |
|---|---|---|
| 真相 | `messages` list | **事件日志** |
| `messages` | 存储 | **投影，用完即弃** |
| 审计 | `executor.audit`（孤立 list） | `permission/decision` 事件，与消息同一时间线 |
| token 用量 | 丢弃 | `request/usage` 事件 |
| 崩溃后 | 全丢 | `Session.load()` 完整恢复 |
| 看历史某刻的上下文 | 做不到 | `derive_messages(upto=N)` |
| 持久化 | 无 | JSONL，每事件一行 |
| `agent_loop` 参数 | `messages` | `session` |

---

## 真实系统里还有什么

- **`assistant/chunk`**：DeepSeek Harness 把流式输出的**每个 chunk** 都写进日志，
  这样 UI 回放能逐 token 重现。我们非流式，一条 `assistant/message` 就够。
- **`surfaceOp`**：真实系统的事件可以携带"对 surface 做什么操作"的指令，
  比如 `{op: "replace", start, end}` —— 上下文压缩就是靠它把一段历史
  替换成摘要，而**不删除任何事件**。s10 会实现一个简化版。
- **`session/end-seed`**：标记"哪些事件来自恢复/fork 的种子，哪些是本次新产生的"。
  没有它，恢复出来的会话和原生会话在日志上完全一样，某些跨生命周期的
  配对检查（比如未闭合的压缩锁）就会误判。
- **持久化后端可替换**：JSONL / SQLite 各是一个 provider。我们只有 JSONL。
- **运行时不变量检查**：真实系统有断言"任何进入模型请求的内容都能从日志重建"，
  在开发期直接炸出来。这条不变量如果只靠自觉，迟早会破。

---

## 自己动手改

1. **手写一个事件日志，然后投影它**
   ```python
   s = Session()
   s.append("user/message", {"content": "你好"})
   s.append("assistant/message", {"text": "你好！", "tool_calls": []})
   print(derive_messages(s))
   ```
   这就是本章的全部秘密：上下文可以被**凭空构造**出来。

2. **把 `tool/call` 加进 SURFACE_EVENTS**
   看看会发生什么（模型会看到重复的工具请求）。这解释了为什么它是 log-only。

3. **把 `permission/decision` 加进 SURFACE_EVENTS**
   实现它的投影逻辑，让模型看到"你刚才被拒绝了，原因是 X"。
   然后想想：这个信息已经在 `tool/result` 里了吗？重复给会有什么代价？

4. **replay 一个真实会话**
   ```sh
   python s05_session_event_log/code.py           # 跑几轮，会生成 session_xxx.jsonl
   python s05_session_event_log/code.py --replay session_xxx.jsonl
   ```

5. **实现 fork**
   写一个 `Session.fork(source, upto)`：复制前 N 条事件到一个新会话。
   （只要几行 —— 因为真相是不可变的事件流，分叉是天然的。
   如果真相是可变的 `messages`，你得深拷贝，还要担心里面的引用。）

6. **故意制造一个崩溃现场**
   在 `run_body` 里 `raise SystemExit`，跑一次，然后 `--replay`。
   你会看到一条 `tool/call` 后面没有 `tool/result` —— 崩溃点一目了然。

---

## 下一章

现在日志里有 19 条事件，但有一件事看不出来：

```
# 3 assistant/message   (请求 1 个工具)
# 7 tool/result
# 8 assistant/message   (请求 1 个工具)
#12 tool/result
```

**哪些事件属于同一次模型调用？哪些属于同一次用户输入？**

日志是平的。一次用户输入可能引发 5 次模型调用、12 次工具执行，
它们在日志里挤成一片，没有任何层次。

这带来几个具体问题：

- 想做"每次用户输入最多花 20 步"的预算 → 没法数，因为"步"没有边界
- 想在"这一轮结束时"做点什么（比如自动提交、检查目标） → 没有"轮结束"这个时刻
- 用户中途插一句话，它属于当前这一轮还是下一轮？

一次用户输入，真的等于一次模型调用吗？

→ [s06 — Turn and Step](../s06_turn_and_step/)
