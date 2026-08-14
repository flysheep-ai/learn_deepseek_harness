# s23 — Self-Extending Harness（自我扩展）

**English version: [README.en.md](README.en.md)**

[s22](../s22_session_lifecycle/) → **s23**（进阶篇·续）

> 模型操作的不再只是文件系统 —— 而是**自己的运行时**。
> 这是 Cordis 论文结论章点名的方向："self-evolving agent harnesses，
> AI 持续生成和替换自己的 harness 组件"。

---

## 上一章留下的问题

s22 把会话生命周期补完了。但回头看 s14：插件是**人**写的，
在**启动时**装配的。模型只能调用人给它的工具，永远改不了
它自己所在的机器。

deepseek-harness 把这件事做成产品特性（agent note
2026-07-08-self-referential-cordis-toolset）：给模型三个工具，
让它**观察并改装自己的运行时**。

## 这一章解决什么

三个工具 + 三个正确性问题：

```
harness_inspect()    观察：我现在由哪些插件/工具/service 组成？
harness_mount(code)  扩展：写一段代码，作为插件挂载进自己，立即生效
harness_unmount(id)  撤回：卸掉它，恢复到我挂载之前
```

dsh 笔记点名的三个正确性问题（比"让模型跑代码"这个机制本身更重要）：

1. **挂载处校验** —— 模型写的注册必须当场爆炸，而不是下次请求组装
   prompt 时才炸。demo 第 3 幕：`required` 引用不存在的 property，
   mount 时被拒，错误信息可行动。
2. **API 可见** —— 模型写的代码要调用它从没见过的 service。
   inspect 提供名字和签名，避免"盲猜方法名"浪费十几步。
3. **完全可处置** —— 模型挂载的一切必须能撤干净：
   模型按需 unmount + 宿主插件卸载时顺带清理，否则长会话积累孤儿监听器。

## 新增的核心概念

### 1. 单 mount 原语，而不是一堆结构化工具

最诱人的替代方案是 `register_tool(name, description, parameters, code)` +
`register_listener` + `register_service`……dsh 拒绝了它：

| 维度 | 结构化注册工具 | 单 mount 原语 |
|---|---|---|
| 能力覆盖 | 只覆盖工具；listener/service/inject 各要一个新工具 —— API 无限膨胀 | 一个词汇（插件）覆盖所有能力，现在和将来 |
| 跨挂载组合 | 表达不了 | 原生的 provide/inject（s20 机制直接复用） |
| 可检视性 | 注册的东西不在插件列表里 | mount 什么，inspect 就显示什么 |

### 2. 临时插件不持久

只活在进程内存：不写文件、不改配置、不跨会话恢复。
**Model-visible ⟺ logged 仍然成立**：mount/unmount 就是
tool/call + tool/result 对，工具集变化由 request/header 记录。
没有为此发明新事件类型。

### 3. 这不是沙箱 —— 是正确性边界

受控命名空间收窄的是**能看到的 API 面**，不是**权限**。
挂载代码可以注册短路监听器停掉 agent 自己的工具派发 ——
信任等级与 bash 相同。真正的沙箱是另一个问题（s15 的 seam 才是它的家）。

## 跑一下

```sh
python s23_self_extending/code.py --demo
```

六幕：inspect → mount 新工具并立即使用 → 坏 schema 当场被拒 →
跨 mount 的 provide/inject 协作 → unmount 到零残留 → 信任边界说明。

## 为什么这样设计

**为什么"自扩展"和前面 22 章不冲突**：mount 出来的插件走的就是 s14 的
PluginContext（注册返回逆 = s19 的可逆效应），依赖关系走 s20 的
provide/inject，卸载走 s21 的生命周期。自我扩展不是新机制，
是**把既有机制的控制权交给模型**。

**为什么临时插件必须可处置**：模型的探索是试错性的。
一次失败的 mount 不能留下半个工具；一次成功的实验要能在
用完后退干净。这就是 s19 可逆效应在自扩展场景的兑现。

**为什么信任等级 = bash**：给了模型写代码的能力却不给 bash 的信任，
等于"给它钥匙但把门焊死"。dsh 的选择是：这是一个 opt-in 的开发工具，
有意识地选择信任，而不是假装安全。

## 与 s22 相比发生了什么

| | s22 | s23 |
|---|---|---|
| 谁改运行时 | 人（fork/resume 时） | **模型（会话进行中）** |
| 新工具 | — | **+3**：inspect / mount / unmount |
| 校验位置 | 事件落盘处 | **挂载处** |
| 新对象 | — | `SelfExtensionPlugin` / `_eval_plugin` |
| 新事件 | — | 零（复用 tool/call 对 + request/header） |

## 真实系统里还有什么

- **生成的 API catalog**：dsh 的 inspect 从生成的目录服务（AST 扫描 +
  freshness 检查），而不是手写表 —— 手写表会在签名变化时漂移。
  教学版直接反射运行时。
- **vm realm + 白名单 façade**：真实的 mount 跑在 node:vm 里，
  traps 把代码导向 cordis 服务，关闭未守卫的 context 逃逸。
- **双 realm instanceof**：host 和 vm 两侧的对象都能被正确识别。
- **canonical tool-output contract**：挂载工具的 schema/输出在 host 侧
  重建并校验，防止模型绕过 `ToolRuntime.execute`。
- **waterfall 短路警告**：工具描述里直接警告模型"挂载的监听器不调
  next() 会停掉你自己的工具派发"。

## 自己动手改

1. 挂载一个**事件监听器**插件（不注册工具），挂在 EVT_TOOL_CALL 上
   统计调用次数，再 unmount 验证它消失。
2. 挂载一个短路监听器（不调 next()），观察 agent 的工具派发如何停摆
   —— 然后理解信任等级的警告。
3. 让 mount 支持"重名插件报错后自动换名重试"的错误信息（可行动的校验）。
4. 把 mounted dict 持久化到 session 事件里，观察"临时插件不跨会话"
   的设计被破坏后会怎样。

## 进阶篇终点（更新版）

s19–s23：可逆效应、反应式依赖、惯性生命周期、会话种子边界、
**自我扩展**。

> Harness 的价值是构建一个可操作的世界。
> deepseek-harness 更进一步：这个世界是**可逆的**（s19）、
> **反应式的**（s20）、**惯性的**（s21）、**可恢复的**（s22）、
> 而且**模型可以亲手改装它**（s23）。
