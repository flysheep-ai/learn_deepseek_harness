#!/usr/bin/env python3
"""s19 — Revertible Effects（可逆效应）

    效应 = 修改 + 它的逆         运行时追踪
    ┌─────────────────┐         ┌──────────────────────────────┐
    │ open() ──▶ fd    │         │ Γ = (state, accumulator)      │
    │ close() ──▶ 回收 │   ───▶  │ effect(f, g):                 │
    └─────────────────┘         │   state ← f(state)             │
                                │   accumulator ← g ∘ accumulator│
                                │ recover():                     │
                                │   state ← accumulator(state)   │
                                └──────────────────────────────┘

这一章回答一个问题，而答案你在 s14 已经**用过**了：

    为什么「注册返回撤销函数」是对的？

因为那正是可逆效应（Cordis 论文 §3.1 的 track/recover）的最简形式。
这一章把 s14 的一行代码讲成一个小理论，并补上它回答不了的部分：

    · LIFO 撤回为什么免费成立？
    · 乱序撤回（从运行中的系统里撤下一个组件）需要什么额外条件？
    · 「恢复」的相等是什么意思？（观察等价）

运行：
    python s19_revertible_effects/code.py --demo
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ══════════════════════════════════════════════════════════════════
# s19 新增：EffectContext —— 可逆效应的最小实现
# ══════════════════════════════════════════════════════════════════


@dataclass
class EffectContext:
    """(state, accumulator)：可逆效应的上下文。

    Cordis 论文里它是 ∂Γ = Γ × (Γ → Γ)：

      · state        当前状态
      · accumulator  到目前为止所有效应的逆的**复合**。
                     把它作用到 state 上，就回到最初的状态。

    关键性质是「健全不变量」（soundness invariant）：

        accumulator(state) == 初始状态

    每次 effect() 都保持它不变 —— 所以**完整恢复是一个结构保证**，
    不是"作者记得写 cleanup"的自觉。
    """

    state: dict[str, Any]
    accumulator: list[Callable[[], None]] = field(default_factory=list)

    def effect(self, forward: Callable[[], None], inverse: Callable[[], None]) -> None:
        """施加一个效应：跑 forward，把 inverse **压栈**进累加器。

        三个设计决策全在这里：

        1. 逆由调用者在**施加处**提供（而不是预先登记）。
           因为一个逆往往只在它作用的那一个状态下成立 ——
           close() 只能关"这个 fd"，必须由 open() 返回。

        2. 逆只需要**单侧**成立：inverse 能把"施加后的状态"带回
           "施加前的状态"即可，不要求 forward(inverse(x)) == x。
           这是工程现实：free 之后堆布局不可能复原，
           但"资源被回收了"已经够了。

        3. 栈序 = LIFO。后施加的先撤回。
           这是免费的：复合的逆按反序复合是**自动成立**的，
           不需要作者写任何代码。（扭曲复合 twisted composition）
        """
        forward()
        self.accumulator.append(inverse)

    def recover(self) -> None:
        """把累加器按 LIFO 全部执行，并清空。

        这就是 s14 的 PluginContext.unload() 做的事：
        `for off in reversed(self._disposers): off()`
        只是没人告诉过你它有个名字，而且它是对的**有数学理由**的。
        """
        while self.accumulator:
            self.accumulator.pop()()

    @property
    def undo_count(self) -> int:
        return len(self.accumulator)


def describe(ctx: EffectContext, label: str = "") -> None:
    print(f"  {label}state={ctx.state}  待撤回的逆={ctx.undo_count} 个")


# ══════════════════════════════════════════════════════════════════
# s19 新增：独立性 —— 乱序撤回需要的额外条件
# ══════════════════════════════════════════════════════════════════


class Component:
    """一个"组件"：持有自己施加的效应的逆。

    单组件场景里 LIFO 就够了。但真实系统里：
      · 多个组件的效应**交错**发生，各自持有自己的逆；
      · 撤回一个组件时，它的逆会遇到**被别的组件移动过的状态**。

    此时逆是否仍成立，是一个**交换性（commutation）**问题。
    """

    def __init__(self, name: str, world: dict[str, Any]) -> None:
        self.name = name
        self.world = world
        self.own_inverses: list[Callable[[], None]] = []

    def do(self, forward: Callable[[], None], inverse: Callable[[], None]) -> None:
        """做一件事：执行，并**把这个逆登记到自己名下**。"""
        forward()
        self.own_inverses.append(inverse)

    def withdraw(self) -> None:
        """撤回：按 LIFO 执行自己的逆。

        如果别的组件在中间改过世界，这里的逆还能正确撤回吗？
        下面两个组件给出了两种答案。
        """
        while self.own_inverses:
            self.own_inverses.pop()()


def demo() -> None:
    print("\033[1m【1】可逆效应的最小机制：effect / accumulator / recover\033[0m")

    box: dict[str, Any] = {"files": 0, "conns": 0}
    ctx = EffectContext(state=box)

    ctx.effect(
        forward=lambda: box.update(files=box["files"] + 1),
        inverse=lambda: box.update(files=box["files"] - 1))
    describe(ctx, "打开一个文件后    ")
    ctx.effect(
        forward=lambda: box.update(conns=box["conns"] + 1),
        inverse=lambda: box.update(conns=box["conns"] - 1))
    describe(ctx, "再开一个连接后    ")

    ctx.recover()
    describe(ctx, "recover() 之后    ")
    print("\033[90m  健全不变量：任何时候 accumulator 作用到 state 上都回到初始状态。")
    print("  LIFO 是免费的 —— 逆按反序复合自动成立，作者没写任何顺序代码。\033[0m")

    print("\n\033[1m【2】对照 s14：你早就用过它了\033[0m")
    print("  s14 PluginContext.unload():")
    print("      for off in reversed(self._disposers): off()")
    print("  = recover()。注册返回撤销函数 = 每个效应自带单侧逆。")
    print("  这一章不是新东西，是给 s14 补上名字和理由。\033[0m")

    print("\n\033[1m【3】乱序撤回：独立性决定成败\033[0m")

    # ── 两个独立的组件 ────────────────────────────────────────────
    world = {"a": 0, "b": 0}
    comp_a = Component("A", world)
    comp_b = Component("B", world)

    # 两个组件的效应交错发生：A 加 a，B 加 b，A 再加 a
    comp_a.do(lambda: world.update(a=world["a"] + 1), lambda: world.update(a=world["a"] - 1))
    comp_b.do(lambda: world.update(b=world["b"] + 1), lambda: world.update(b=world["b"] - 1))
    comp_a.do(lambda: world.update(a=world["a"] + 1), lambda: world.update(a=world["a"] - 1))
    print(f"  交错执行后：{world}")
    print("  撤回 A（此时 B 的效应还在世界里）：")
    comp_a.withdraw()
    print(f"    → {world}  \\033[32mA 的贡献被干净地撤掉了，B 的还在\\033[0m")
    print("  \\033[90m  为什么成功？两个组件的变换互相交换（各写各的 key）。")
    print("  这就是论文里「独立性」（independence）的最简形式：")
    print("  一个效应的所有变换 与 另一个效应的所有变换 两两交换。\\033[0m")

    # ── 两个不独立的组件 ──────────────────────────────────────────
    print("\n  现在换成两个**不独立**的组件 —— 它们操作同一个 key：")
    world2 = {"counter": 0}
    c1 = Component("C1", world2)
    c2 = Component("C2", world2)

    # C1 的逆（-1）是**完全正确的逆**。问题不在逆，在交错。
    c1.do(lambda: world2.update(counter=world2["counter"] + 1),
          lambda: world2.update(counter=world2["counter"] - 1))
    c2.do(lambda: world2.update(counter=world2["counter"] * 10),
          lambda: world2.update(counter=world2["counter"] // 10))
    print(f"  交错执行后：{world2}")
    print("  \\033[90m  「C1 从未发生」的世界应该是 C2 单独作用：0 × 10 = 0。\\033[0m")
    c1.withdraw()
    print(f"  撤回 C1 后：{world2}  \\033[31m← 是 9，不是 0！\\033[0m")
    print("  \\033[90m  C1 的逆本身没错（-1 确实是 +1 的逆）—— 错在它")
    print("  作用在一个被 C2 移动过的状态上。逆和正向变换都必须与")
    print("  对方的变换交换（commute），乱序撤回才安全。\\033[0m")
    print("  独立性失败的组件不能安全乱序撤回 —— 只能：")
    print("    · 按施加顺序整体撤回（accumulator 的 LIFO），或")
    print("    · 用声明（coeffect）强制一个先停 —— 这就是 s20 要讲的。")

    print("\n\033[1m【4】「恢复」的相等是什么意思？\033[0m")
    print("  物理状态不可能逐位复原：free 不回堆布局，生成的名字不复原。")
    print("  所以恢复的「回到初始」读作**观察等价**：")
    print("    两个状态相关 ⟺ 没有观察者能区分它们。")
    print("  而观察者被赋予的能力，正是 s20 的 coeffect ——")
    print("  依赖定义了「恢复到什么粒度就算数」。\\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章的三句话：")
    print("  1. 每个原子效应自带单侧逆；复合的逆自动按反序复合（免费 LIFO）")
    print("  2. 运行时把逆压进 accumulator，恢复 = 跑一遍 accumulator")
    print("  3. 乱序撤回需要独立性（交换性）；没有它，就回到 LIFO 或等 s20 的级联纪律")
    print("参考：docs/cordis-paper-spatiotemporal-composability.md §三（Cordis 论文 §3.1）\033[0m")


if __name__ == "__main__":
    demo()
