# s10 — Context Compaction

**English version: [README.en.md](README.en.md)**

[s09](../s09_subagent/) → **s10** → [s11](../s11_task_system/) → … → s18

> 上下文撑爆了怎么办？
>
> 但真正的问题是：既然 s05 说日志是唯一真相、messages 只是投影，
> 那压缩到底压的是**谁**？

---

## 上一章留下的问题

s09 让**新的**大块内容不进主上下文。但主上下文自己还在长：

```
[step 1]  ctx=8/600 (1%)
[step 2]  ctx=419/600 (69%)
[step 3]  ctx=856/600 (142%)   ← 爆了
```

最直接的做法是截断：

```python
messages = messages[-6:]
```

**这会立刻炸。** demo 第二部分把它跑给你看：

```
naive_truncate(keep=6) 之后 （6 条 / 108 tokens）
  tool       1  VERSION = "0.1.0"…  ← 孤儿！配对的 tool_call 被切掉了
  assistant  +1 calls
  tool       已编辑 app.py
  ...
```

第一条 `tool` 消息的 `tool_call_id` 找不到对应的 `tool_call` ——
它在被切掉的那条 `assistant` 消息里。模型侧直接报错，整个请求失败。

而且截断还有第二个问题：**遗忘**。模型不知道自己 20 步之前干了什么，会重复劳动。

---

## 这一章解决什么

**压的是投影，不是日志。**

```
事件日志（append-only，一条都不删）
  #4  tool/result   （auth/middleware.py 全文）  ┐
  #6  tool/result   （auth/tokens.py 全文）      ├─ 被 shadow
  #10 tool/result   （api/deps.py 全文）         ┘
  ...
  #44 compaction/summary  { shadowed_seqs: [4,6,10,…], summary: "…" }   ← 新追加
  ...
        │
        │ derive_messages()  ← 跳过被 shadow 的，在范围起点插入摘要
        ▼
messages：[摘要] + 最近的消息
```

> **Shadow，不是 delete。**
> 压缩改变的是"怎么看历史"，不是"历史是什么"。

---

## 新增的核心概念

### 1. 三条事件构成一个括号

```
compaction/start  →  （调模型生成摘要，这里可能崩）  →  compaction/summary  →  compaction/end
```

为什么要三条而不是一条？

因为中间那次模型调用是**唯一会失败的地方**。留下一条没有配对 `end` 的 `start`，
就是"压缩做到一半挂了"的铁证。

和 s05 里 `tool/call` 先于执行落盘是同一个道理：
**先记意图，后记结果，崩溃现场才不会说谎。**

### 2. 安全边界：只在 `step/end` 处切

```python
def find_safe_boundary(session, keep_tokens):
    cuts = [e.seq for e in events if e.type == EV_STEP_END]
```

安全的定义只有一条：

> 被遮蔽的范围里，每一个 `tool_call` 都必须连它的 `tool_result` 一起被遮蔽。

怎么保证？**只在 `step/end` 处切。**

一个 step 的定义是"一次模型请求 + 它引发的工具执行"，所以 step 结束时
这一步的所有 `tool_call` 都已经有了配对的 result。在 `step/end` 处切，
配对天然完整 —— 不需要额外去数 id。

**这是 s06 那个看起来只是"分组"的 turn/step 结构第一次产生实际收益：
它给了日志一组天然安全的切分点。**

如果没有 s06，你就得写一个"扫描所有 tool_call id、找出配对边界"的函数，
而且每加一种新事件都要维护它。

### 3. 再压缩要**吸收**上一次的摘要

第一版实现跑出来是这样的：

```
user  [以下是之前 3 条消息的压缩摘要]…
user  [以下是之前 2 条消息的压缩摘要]…
user  [以下是之前 2 条消息的压缩摘要]…
```

三段摘要并排堆着，越压越乱。

正确做法：每条 `compaction/summary` 带一个 `supersedes` 字段，
记录它吸收了哪些旧摘要；`collect_shadows` 跳过被吸收的：

```
发生了 3 次压缩，但只有最后一条摘要生效：
  生效摘要 #44，遮蔽 7 条消息，吸收了 2 条旧摘要
  已被吸收 #22（仍在日志里，只是不再参与投影）
  已被吸收 #33（仍在日志里，只是不再参与投影）
```

而且摘要模型的输入里会带上**上一次的摘要**：

```python
body = (f"[上一次压缩的摘要]\n{carry}\n\n" if carry else "") + _render_for_summary(old_msgs)
```

这样"已查明的事实"能一代代传下去，而不是每压一次丢一点。

### 4. 摘要要保留什么

```python
SUMMARIZE_SYSTEM = (
    "把下面这段历史浓缩成一段简短的交接说明，必须保留：\n"
    "  1. 用户最初的目标\n"
    "  2. 已经查明的关键事实（文件名、行号、结论）\n"
    "  3. 已经做出的修改\n"
    "  4. 还没做完的事\n"
    "不要复述工具的原始输出。"
)
```

摘要不是"工具输出的缩写"，是**交接说明**。

想象你把工作交给另一个人：你会告诉他目标是什么、你查到了什么、
你改了什么、还剩什么 —— 而不是把你读过的文件念一遍。

---

## 最小架构图

```
   每个 step 之前
        │
        ▼
   estimate_tokens(derive_messages(session)) > 限额 × 0.75 ?
        │ 是
        ▼
   ┌─────────────────── compact() ───────────────────┐
   │ find_safe_boundary()  ← 只取 step/end 的 seq     │
   │ fresh = 边界前未遮蔽的 SURFACE 事件               │
   │ 收益够吗？ estimate_tokens(fresh) ≥ 限额×0.15     │
   │        │ 够                                      │
   │ compaction/start                                 │
   │   摘要模型（输入 = 上次摘要 + fresh）  ← 可能失败  │
   │ compaction/summary { shadowed_seqs, supersedes } │
   │ compaction/end                                   │
   └──────────────────────┬──────────────────────────┘
                          ▼
   derive_messages()：跳过 shadowed，在范围起点插摘要
```

---

## 跑一下

```sh
python s10_context_compaction/code.py --demo
python s10_context_compaction/code.py --demo --debug
```

`--debug` 里能看到压力和压缩的对应关系：

```
→ model request   messages=3  ctx=419/600 (69%)
· compaction     3 条消息被遮蔽  856 → 488 tokens
→ model request   messages=3  ctx=488/600 (81%)
· compaction     5 条消息被遮蔽  795 → 344 tokens
→ model request   messages=3  ctx=344/600 (57%)
```

demo 把窗口调成 600 tokens（真实是 128k/200k），这样几步就能看到效果。

---

## 为什么这样设计

### 为什么在 75% 就压，而不是超了再压

因为超限是一个**请求失败**。失败发生时你已经浪费了一次调用（钱 + 延迟），
而且要处理"这次请求怎么重试"的复杂路径。

提前在 75% 压，代价只是一次便宜的摘要调用。

（真实系统两条路都留：`pre-step` 时按压力主动压，
以及请求返回 context-overflow 错误时被动压。第二条是保险，不是主路径。）

### 为什么"值不值得压"要按 token 算，不按条数算

我第一版写的是：

```python
if len(to_shadow) < 4:
    return False        # ❌
```

这是错的。4 条 `read` 结果和 4 条 `"已编辑 x.py"` 差着两个数量级。

压缩要花一次模型调用，所以门槛应该定在**收益**上：

```python
saving = estimate_tokens(_project_range(session, fresh))
if saving < CONTEXT_LIMIT_TOKENS * 0.15:
    return False        # ✅
```

（这个 bug 是我写 demo 时真跑出来的：第一次能压，后面两次因为条数不够被跳过，
上下文一路涨到 945/600。换成收益判据之后就正常了。）

### 为什么摘要插在范围的**起点**，不是末尾

```python
if ev.seq in anchors:                    # anchors[min(shadowed_seqs)]
    messages.append({"role": "user", "content": anchors[ev.seq]})
```

摘要讲的是"这之前发生了什么"，它就该出现在那个位置，保持时间顺序。

插在末尾的话，模型会看到"最近的消息……然后是一段讲更早的事的摘要"，
时间线是乱的。

### 为什么子 Agent 不压缩

```python
if SUMMARIZER is not None and prompt_registry is None:
```

子 Agent 本来就短命（s09 那个 explorer 跑 4 步就结束了）。
为它花一次额外的模型调用，收益不足以抵消成本。

**优化要看生命周期。** 同一个机制对主 Agent 是必需的，对子 Agent 是浪费。

### 压缩之后还能时间旅行吗

能。这是 shadow 而非 delete 最直接的红利：

```python
derive_messages(session, upto=23)     # 仍然是压缩前的样子
```

因为 `collect_shadows(session, upto)` 也受 `upto` 约束 ——
在第 23 号事件的时刻，那条 `compaction/summary`（#44）还不存在。

**如果压缩是"删掉旧消息"，这一节就不存在了。**

---

## 与上一章相比发生了什么

| | s09 | s10 |
|---|---|---|
| 上下文超限 | 直接失败 | **主动压缩** |
| 压缩方式 | — | shadow 投影，日志不删 |
| 切分安全性 | — | 只在 `step/end` 切，配对天然完整 |
| 多次压缩 | — | 后一次**吸收**前一次（`supersedes`） |
| 新事件 | — | `compaction/start` `/summary` `/end`（log-only） |
| `derive_messages` | 只过滤 SURFACE | **+ 跳过 shadowed，插入摘要** |
| 触发时机 | — | 每个 step 之前，75% 阈值 |
| 时间旅行 | 可以 | **仍然可以** |

---

## 真实系统里还有什么

- **`surfaceOp`**：DeepSeek Harness 的摘要不是一个特殊事件类型，
  而是一条普通 `user/message` 携带 `surfaceOp: {op: "replace", start, end}`。
  这样"投影层"只需要理解一种操作，而不是每加一个压缩策略就改一次 `deriveMessages`。
  我们的 `shadowed_seqs` 是它的简化版。
- **先剪枝、后摘要**：真实系统会先做**无模型**的裁剪（比如把老的 `tool_result`
  换成 "(已省略)"），如果这样就够了，就不花那次摘要调用。
- **压缩锁**：`compaction/start` 是一个记录在日志里的锁。
  一个未闭合的 start 会阻塞后续压缩入口 —— 但要能区分"上个生命周期遗留的"
  和"当前正在进行的"，这就是 dsh 里 `session/end-seed` 事件存在的原因之一。
- **真正的 tokenizer**：我们用 `len//4` 估算，会估错。估少了会白压，
  估多了会仍然超限。
- **压缩时机的其他触发点**：用户手动 `/compact`（我们有）、
  会话恢复时、模型返回 overflow 错误时。

---

## 自己动手改

1. **把安全边界破坏掉**
   把 `find_safe_boundary` 的候选从 `EV_STEP_END` 改成 `EV_TOOL_CALL`，
   跑 demo，看 `压缩后的投影` 里出现 `← 孤儿！`。
   这是理解"为什么必须在 step/end 切"最快的方式。

2. **关掉摘要吸收**
   把 `supersedes` 那行删掉，看三段摘要怎么堆在一起。

3. **改摘要提示词**
   把 `SUMMARIZE_SYSTEM` 改成"用一句话概括"，然后（用真实模型）
   观察模型压缩后是不是开始重复它已经做过的事。
   **摘要里丢掉什么，模型就忘掉什么。**

4. **手动压缩**
   ```sh
   python s10_context_compaction/code.py     # 真实模型
   > /ctx        # 看当前上下文
   > /compact    # 手动压一次
   > /ctx        # 再看
   ```

5. **验证时间旅行**
   ```python
   before = derive_messages(session, upto=20)
   after  = derive_messages(session)
   # before 里没有摘要，after 里有
   ```

6. **量一下压缩的信息损失**
   压缩前后各问模型同一个问题（"你刚才读了哪些文件？"），对比答案。

---

## 下一章

现在给 Agent 一个大任务：

> "把这个项目的所有 print 换成 logging，加上类型标注，然后跑测试确认没坏。"

模型会说："好的，我分三步：① 替换 print ② 加类型标注 ③ 跑测试。"

然后它开始干第一步。20 个 step 之后，上下文压缩了两次。

**它忘了第 3 步。**

因为"我打算做三件事"这句话只存在于**模型说过的一段文字**里。
它是上下文的一部分，会被压缩、会被稀释、会在多轮之后被淹没。

摘要提示词里我们写了"还没做完的事"——但那是在**祈祷**模型每次都写对，
而不是**保证**。

计划这种东西，能不能不放在模型脑子里，而是放在 Harness 手上？

→ [s11 — Task System](../s11_task_system/)
