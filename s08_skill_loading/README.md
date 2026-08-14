# s08 — Skill Loading

**English version: [README.en.md](README.en.md)**

[s07](../s07_prompt_assembly/) → **s08** → [s09](../s09_subagent/) → … → s18

> 知识应该提前塞进 prompt，还是等模型要的时候再给？
>
> 这个问题的答案，决定了你的 Agent 能不能长出第 10 个能力。

---

## 上一章留下的问题

s07 让你可以随便加 section。于是很自然会这么做：

```python
@prompts.section("git_guide", 60)
def _git_guide(ctx): return open("guides/git.md").read()        # 3000 字
@prompts.section("python_style", 61)
def _py_style(ctx): return open("guides/python.md").read()      # 2500 字
@prompts.section("debugging", 62)
def _debug(ctx): return open("guides/debugging.md").read()      # 2000 字
```

然后你发现两件事：

**1. 每一次请求都在为它们付费。**
用户问"config.py 里 TIMEOUT 是多少"，跟 git 工作流一点关系都没有，
但那 7500 字照样发出去了。10 个知识块 = 每次请求 30000 字。

**2. 更糟的是注意力被稀释。**
这不只是钱的问题。模型要在 30000 字里找到当前真正相关的那 500 字。
塞得越多，它越容易被无关内容带偏。

---

## 这一章解决什么

**Progressive Disclosure（渐进式披露）**：

```
常驻 prompt（便宜，总是在）        按需加载（贵，只在需要时）
┌──────────────────────────┐      ┌──────────────────────────┐
│ 可用技能：                │      │ # 系统化排查              │
│ - git-workflow：git 规范  │ ───▶ │ 1. 复现：先跑一次…        │
│ - python-style：代码规范  │      │ 2. 读完整报错…            │
│ - debugging：排查方法     │      │ …                        │
│         196 字符          │      │        494 字符           │
└──────────────────────────┘      └──────────────────────────┘
                                   只有被 skill("debugging") 加载后才出现
```

demo 第一部分把账算给你看：

```
全部塞进 prompt：1610 字符/次请求
只放目录：       196 字符/次请求  （12%）
```

真实项目里技能正文动辄几千字，这个比例会更悬殊。

---

## 新增的核心概念

### 1. Skill：两段式结构

```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str      # ← 这两个常驻 prompt
    path: Path

    @property
    def body(self) -> str:            # ← 这个按需读
        return _strip_frontmatter(self.path.read_text()).strip()
```

磁盘上就是一个目录 + 一个带 frontmatter 的 Markdown：

```
skills/
  debugging/SKILL.md
  git-workflow/SKILL.md
  python-style/SKILL.md
```

```markdown
---
name: debugging
description: 排查失败测试和运行时错误的系统方法。测试挂了、报错看不懂时读。
---

# 系统化排查
...
```

**`body` 是 property 而不是字段**，这不是风格问题：如果构造时就把正文读进内存，
10 份技能就是 10 份常驻内存，"渐进"只剩了个名字。

### 2. description 是给模型的**路由信息**

```python
if not desc:
    print(f"[skill] 跳过 {name}：缺少 description")
    continue
```

没有 description 的技能对模型是**不可见**的 —— 它无从判断什么时候该用。

所以 description 的写法很关键。对比：

```
❌ description: 调试相关
✅ description: 排查失败测试和运行时错误的系统方法。测试挂了、报错看不懂时读。
```

后者写清了**触发条件**。模型是靠这句话决定要不要加载的，
它就是这份知识的路由表。

### 3. SkillRegistry：和 ToolRegistry 是兄弟，但职责不同

```
ToolRegistry   模型能做什么   （action space）
SkillRegistry  模型该怎么做   （knowledge）
```

把知识做成工具是个常见的错误设计：那样每份知识都占一个 tool schema 的位置，
而且模型必须"调用"才能读到。**技能是内容，工具是能力。**

我们只有一个 `skill` 工具（取用方式），技能本身不是工具。

### 4. 正文走 inbox 注入，不走工具结果

```python
def run_skill(name: str) -> str:
    ...
    INBOX.put(f"[已加载技能：{name}]\n\n{body}", source="skill")
    return f"已加载技能 {name}（{len(body)} 字符），内容将在下一步进入上下文。"
```

工具**只返回一句确认**，正文在下一步才进上下文。为什么绕这一圈？

**因为语义不同。** 工具结果是 `role:"tool"` 的消息，模型会把它当成一个**观察**
（"我执行了 X，得到了 Y"）。而技能正文是**指令**，它应该像用户说的话一样有约束力。

`--debug` 里能清楚看到这条路径：

```
[step 1]  claimed=0
    ← model reply     tool_calls=1 [skill]
    · tool result     skill ok 44B          ← 只有确认，44 字节
[step 2]  claimed=1 (skill)                 ← 正文在这里被认领
    → model request   messages=8 system=517chars
```

而且它**复用了 s06 已经建好的 inbox 机制**，没有发明新通道。
技能内容、后台任务完成通知（s12）、子 Agent 结果（s09）……
这些"Harness 想让模型知道的事"来源各异，但只有一条入口。

---

## 最小架构图

```
  skills/*/SKILL.md
        │ discover()
        ▼
  ┌──────────────┐    catalog()    ┌──────────────────┐
  │SkillRegistry │────────────────▶│ PromptSection    │──▶ system prompt
  │              │  name + desc    │ "skills"（常驻）  │      （便宜）
  └──────┬───────┘                 └──────────────────┘
         │
         │ get(name).body           模型调用 skill("debugging")
         │        ▲                          │
         │        └──────────────────────────┘
         ▼
      Inbox.put(正文, source="skill")
         │
         ▼  下一个 step 认领
      user/message（SURFACE）──▶ 进入模型上下文
```

---

## 跑一下

```sh
python s08_skill_loading/code.py --demo
python s08_skill_loading/code.py --demo --debug
```

demo 用两个问题做对照：

**问题 A（用不上技能）**："config.py 里 TIMEOUT 是多少？"
→ 模型直接 `read`，`已加载技能：（无）`。**一个字都没多付。**

**问题 B（需要技能）**："测试挂了，帮我按团队的方法排查一下"
→ 模型看到目录里有 `debugging`，**自己决定**调用 `skill("debugging")`，
然后按技能里的第 1 步"先复现"去跑 pytest。

注意"自己决定"这四个字。Harness 里没有任何一行代码写着
"如果用户提到测试就加载 debugging 技能"。

---

## 为什么这样设计

### 为什么目录必须**总是**在

渐进式披露有两半，缺一不可：

- 目录必须**总是**在 —— 否则模型永远想不到去要
- 正文必须**按需**给 —— 否则目录就没意义了

section 里那句提示也是必需的：

```
下面是可按需加载的知识。**只列了标题**，需要时用 skill 工具加载全文。
```

不写这句，模型会以为目录就是全部内容，然后基于一句话的描述硬猜细节。

### 为什么 loaded_skills 不按轮重置

```python
rt.files_read = []          # 按轮重置
# loaded_skills 不重置
```

因为技能正文一旦注入就**永久留在上下文里** —— 它是一条 `user/message`，
日志里抹不掉。重置只会诱导模型重复加载同一份内容。

`files_read` 不同：它只是一个"本轮别重复读"的提示，跨轮重置是合理的。

**状态的重置周期，应该由这个状态的实际生命周期决定**，不能一刀切。

### 为什么重复加载要挡掉

```python
if name in RT.loaded_skills:
    return f"技能 {name} 已经加载过了，内容就在上文，不要重复加载。"
```

模型确实会重复请求同一份技能（尤其在长会话里，它可能忘了自己读过）。
不挡的话，同一份 500 字会在上下文里出现三次。

注意这条挡回去的信息同样是**可行动**的：告诉它"内容就在上文"，
它就知道该往回翻，而不是换个名字再试一次。

### 这算不算 Harness 替模型做决策？

不算，而且这条边界值得仔细看：

```python
# ✅ Harness 做的：把目录摆出来，提供 skill 工具
@prompts.section("skills", 45)
def _skills(ctx): return "可用技能：\n- debugging：…"

# ❌ Harness 没做的：
if "测试" in user_input:
    load_skill("debugging")
```

Harness 提供**能力和信息**，"什么时候用哪份知识"是模型的判断。

如果你写了那个 `if`，就等于把模型的判断力换成了一个关键词匹配 ——
用户说"这个用例跑不过"就匹配不上了。

---

## 与上一章相比发生了什么

| | s07 | s08 |
|---|---|---|
| 知识的位置 | 全塞进 prompt section | **目录常驻，正文按需** |
| 每次请求的知识成本 | 全量（1610 字符） | 目录（196 字符，12%） |
| 谁决定加载什么 | — | **模型** |
| 新对象 | — | `Skill` / `SkillRegistry` |
| 新工具 | 6 个 | **7 个**（+`skill`） |
| 新事件 | — | `skill/load`（log-only） |
| 正文进上下文的路径 | — | inbox 注入 → `user/message` |

---

## 真实系统里还有什么

- **多 provider**：真实 Harness 的 `ctx.skills` 背后可以挂本地目录、
  内置包、远程仓库多个 provider，读取时把各层的目录合并，
  同名按"最近的层赢"。我们只有本地目录一个。
- **per-agent 技能集**：不同的 agent 可以看到不同的技能目录
  （子 Agent 通常只需要其中一两份）。s09 会做类似的事情，但那里限制的是工具。
- **技能里的可执行资源**：真实的 SKILL.md 常常附带脚本、模板、参考文件，
  加载技能等于把一整个目录挂进工作区。我们只加载正文。
- **技能失效与缓存**：目录变了要通知消费者刷新。我们每步重新 `catalog()`，
  简单但每次都扫内存里的 dict（没有重新扫盘）。
- **同一个思路的其他应用**：文件内容也可以渐进式披露 ——
  先给 `read(limit=50)` 的前 50 行，模型觉得不够再要全文。
  MCP 的工具目录、代码库的符号索引，都是这个模式。

---

## 自己动手改

1. **写一份自己的技能**
   ```sh
   mkdir -p s08_skill_loading/skills/sql-review
   ```
   写好 frontmatter，重跑 demo，看它出现在目录里。**你没有改任何代码。**

2. **把 description 删掉**
   看它被跳过并打印警告。想想：为什么"没有描述的技能"比"不存在的技能"更糟？
   （因为它在磁盘上，你以为它生效了。）

3. **对比两种做法的 token**
   把三份技能全部改成常驻 section（`@prompts.section` 直接返回 `body`），
   跑那个"TIMEOUT 是多少"的问题，对比 `--debug` 里的 `system=NNNchars`。

4. **让技能正文走工具结果**
   把 `run_skill` 改成 `return body`，去掉 inbox 注入。
   跑起来是能用的 —— 但去 `--debug` 里看，它变成了 `role:"tool"` 的消息。
   想想模型对"我执行了一个工具得到这段文字"和"有人告诉我这段规则"
   会不会有不同的服从度。

5. **观察目录的路由效果**
   把 `debugging` 的 description 改成含糊的"调试相关"，重跑问题 B。
   （用真实模型时，模型很可能就不去加载它了。）

---

## 下一章

现在有一个新问题。假设用户说：

> "这个代码库里，鉴权逻辑分散在哪些文件？"

模型会 `grep`、`glob`、`read` 十几个文件。这些搜索结果 —— 可能是 60000 token
的原始文件内容 —— **全部灌进了主上下文**，而且**永远留在那里**。

最终模型只需要给出一句结论："鉴权在 auth/middleware.py 和 api/deps.py 里。"

但那 60000 token 的垃圾会跟着你走完整个会话：

- 后面每一步都要重新发送它们（钱）
- 后面每一步模型都要在里面找重点（注意力）
- 上下文很快就撑爆（s10 的问题）

能不能让"搜索"这件事在**另一个上下文**里进行，只把结论带回来？

如果可以，那个"另一个上下文"里的 Agent，应该有和主 Agent 一样的权力吗？

→ [s09 — Subagent](../s09_subagent/)
