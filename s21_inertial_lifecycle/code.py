#!/usr/bin/env python3
"""s21 — Inertial Lifecycle（目标视图驱动的惯性生命周期）

    target view（应该是什么样）  vs  committed view（实际是什么样）

      target = ⊥          不该跑        committed = ⊥    没在跑
      target = 解析映射    该跑成这个依赖组合

      两者比较驱动一切：
        相等 → 不动
        不等 → 开始转换（Reload / Unload）

    惯性（inertia）：转换一旦开始，就**跑完再响应**新的目标变化。
    跑完时再比一次：目标又变了？→ 链式进入下一个转换。

这一章把 s06 的 turn/step、s12 的异步 job、s20 的级联卸载
拼成 deepseek-harness 文档里最精彩的机制之一（Cordis 论文 §4.2/§4.3）：

    · 生命周期由「应该」与「实际」的比较驱动，而不是由调用命令驱动
    · 中间态：INACTIVE → RELOADING → ACTIVE → UNLOADING → INACTIVE
    · 失败先恢复再记录：失败组件对状态的贡献为零，且不传染兄弟
    · 转换在途时目标变了：链式切换，绝不半途响应

运行：
    python s21_inertial_lifecycle/code.py --demo
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ══════════════════════════════════════════════════════════════════
# s21 新增：Fiber —— 带中间态的生命周期
# ══════════════════════════════════════════════════════════════════


@dataclass
class Fiber:
    """一个组件的运行实例（Cordis 论文里的 fiber）。

    核心结构是**两个视图**：

      target    应该是什么样：⊥ = 不该跑；否则是"每个声明 key 由谁提供"的解析
      committed 实际激活时承诺的解析

    所有规则都由两者的比较驱动 —— 这就是"反应式"拿到生命周期
    之后的形态：不是你调用了 load/unload，而是"实际≠应该"本身
    触发了转换。
    """

    name: str
    inject: frozenset[str]
    setup: Callable[[], None]          # 效应函数：激活时执行
    teardown: Callable[[], None]       # 它的逆（LIFO）
    state: str = "INACTIVE"            # INACTIVE | RELOADING | ACTIVE | UNLOADING
    target: Any = None                 # 目标视图（⊥ 用 None 表示）
    committed: Any = None              # 承诺视图
    accumulator: list[Callable[[], None]] = field(default_factory=list)
    error: str | None = None           # 失败原因（先恢复，再记录在这里）
    history: list[str] = field(default_factory=list)

    def compute_target(self, providers: dict[str, str], retired: bool) -> Any:
        """目标视图：退休 → ⊥；声明未满足 → ⊥；否则 = 解析映射。"""
        if retired:
            return None
        resolution = {k: providers.get(k) for k in self.inject}
        return None if any(v is None for v in resolution.values()) else resolution

    def log(self, msg: str) -> None:
        self.history.append(msg)


@dataclass
class Runtime:
    """驱动 fiber 集合的小运行时。

    教学版的转换是同步的，但保留了**惯性**语义：
    一次转换（reload / unload）从头跑到底，中途不响应
    目标变化；跑完时再比对一次目标，变了就链式切换。
    """

    def __init__(self) -> None:
        self.fibers: dict[str, Fiber] = {}
        self.providers: dict[str, str] = {}
        self.retired: dict[str, bool] = {}
        self.trace: list[str] = []

    def add(self, fiber: Fiber) -> None:
        self.fibers[fiber.name] = fiber
        self.retired[fiber.name] = False
        self.refresh(fiber.name)

    def retire(self, name: str) -> None:
        """退休（retire）与移除（remove）分离：retire 是**请求**，
        生命周期规则负责执行到位。提前移除会丢掉累加器而泄漏。"""
        self.retired[name] = True
        self.refresh(name)

    def provide(self, owner: str, key: str) -> None:
        self.providers[key] = owner
        self.refresh_all()

    def unprovide(self, owner: str, key: str) -> None:
        if self.providers.get(key) == owner:
            del self.providers[key]
        self.refresh_all()

    def refresh_all(self) -> None:
        for name in list(self.fibers):
            self.refresh(name)

    # ── 两个视图的比较：生命周期唯一的引擎 ────────────────────────
    def refresh(self, name: str) -> None:
        """重算 target；如果和 committed 不同且没有转换在途，启动转换。

        注意「转换在途」时的行为 —— 这就是**惯性**：
        不打断，等它跑完；跑完的那一刻会再比一次。
        """
        f = self.fibers[name]
        f.target = f.compute_target(self.providers, self.retired[name])
        if f.error:
            # 失败的 fiber 不重入生命周期（论文：L-Begin 的前提是
            # INACTIVE(⊥)，错误结果 INACTIVE(ξ) 不会重新开始）——
            # 它对环境不变的重试只会再失败一次，等待人的干预。
            return
        if f.state in ("RELOADING", "UNLOADING"):
            return          # 在途：记下目标，转换完成时处理
        if f.target == f.committed:
            return          # 实际 == 应该：不动
        if f.target is not None:
            self._begin_reload(f)
        else:
            self._begin_unload(f)

    # ── 转换：惯性（跑完再响应）────────────────────────────────────
    def _begin_reload(self, f: Fiber) -> None:
        f.log(f"→ RELOADING（target={f.target}）")
        f.state = "RELOADING"
        self.trace.append(f"reload:{f.name} 开始")

        # 效应执行。教学版简化为一步；真实系统是**迭代器**
        # （Cordis 论文 §4.3.2），每一步之后检查目标是否过期
        # （staleness check）—— 过期就中止，部分构建的效应靠累加器回滚。
        try:
            f.setup()
            f.accumulator.append(f.teardown)   # 可逆效应：逆进累加器
        except Exception as e:
            # ── 失败：先恢复，再记录 ─────────────────────────────
            # L-Raise：把已构建的累加器全部跑掉（回到"什么都没装"），
            # 然后进入 UNLOADING 带着错误出口。失败走**同一条**
            # unload 路径 —— 所以失败组件对状态的贡献为零。
            self._recover_accumulator(f)
            f.error = f"{type(e).__name__}: {e}"
            f.log(f"✗ 失败：{f.error}（已恢复，记录在 fiber 上）")
            self._finish_unload(f, error=True)
            return

        # 惯性：跑完了。目标还是它吗？
        if f.target is not None and f.state == "RELOADING":
            f.committed = f.target
            f.state = "ACTIVE"
            f.log(f"● ACTIVE（committed={f.committed}）")
            self.trace.append(f"reload:{f.name} 完成")
        elif f.target != f.committed:
            # 目标在途变了 → 链式切换进 unload
            self._chain_into_unload(f)
        else:
            self._chain_into_unload(f)

    def _begin_unload(self, f: Fiber) -> None:
        # L-Leave：先标记 UNLOADING（先于一切逆）——
        # 这正是 s20 的「先停供」：依赖者在这一刻就重算了。
        f.state = "UNLOADING"
        f.log(f"→ UNLOADING（先停供，逆稍后）")
        self.trace.append(f"unload:{f.name} 开始")
        # 教学版：等待依赖者的守卫简化为「先看一圈」，
        # s20 已经完整演示过三段顺序。
        self._recover_accumulator(f)
        self._finish_unload(f)

    def _recover_accumulator(self, f: Fiber) -> None:
        while f.accumulator:
            f.accumulator.pop()()

    def _finish_unload(self, f: Fiber, error: bool = False) -> None:
        if not error:
            f.committed = None
        f.state = "INACTIVE"
        f.log(f"○ INACTIVE" + (f"（错误：{f.error}）" if f.error else ""))
        self.trace.append(f"unload:{f.name} 完成")
        # 惯性收尾：unload 跑完后再比一次目标 —— 又变了？
        # 目标还是非 ⊥ → 链式回到 reload。
        if f.target is not None and not error:
            self._begin_reload(f)

    def _chain_into_unload(self, f: Fiber) -> None:
        self.trace.append(f"reload:{f.name} 链式切换 → unload")
        self._begin_unload(f)


# ══════════════════════════════════════════════════════════════════
# 演示
# ══════════════════════════════════════════════════════════════════


def demo() -> None:
    print("\033[1m【1】两个视图的比较驱动一切\033[0m")
    rt = Runtime()
    setup_calls: dict[str, int] = {}

    def mk(name, inject=()):
        def setup():
            setup_calls[name] = setup_calls.get(name, 0) + 1

        def teardown():
            setup_calls[name] -= 1

        rt.add(Fiber(name, frozenset(inject), setup, teardown))

    mk("db")
    mk("cache", inject={"db"})
    print("  注册 db 和 cache（cache 依赖 db）。")
    print("  cache 的 target = ⊥（db 还没人提供）→ 生命周期没有动作：")
    print(f"    cache.state = \\033[36m{rt.fibers['cache'].state}\\033[0m")

    print("\n\033[1m【2】目标出现，转换自动开始\033[0m")
    rt.provide("db", "db")
    f = rt.fibers["cache"]
    print(f"  提供 db 之后，cache 的 target 从 ⊥ 变成解析映射 → 自动 reload：")
    print(f"    state = \\033[36m{f.state}\\033[0m  committed = {f.committed}")
    print(f"  历史：")
    for h in f.history:
        print(f"    \\033[90m{h}\\033[0m")

    print("\n\033[1m【3】惯性：转换在途时目标变了，跑完再链式响应\033[0m")
    rt.provide("db", "db")
    f2 = rt.fibers["cache"]
    print(f"  cache 目前 ACTIVE。撤走 db 的提供：")
    rt.unprovide("db", "db")
    print(f"  历史（看最后几行）：")
    for h in f2.history[-4:]:
        print(f"    \\033[90m{h}\\033[0m")
    print(f"  最终 state = \\033[36m{f2.state}\\033[0m")
    print("  \\033[90m  注意：卸载跑完后目标仍是 ⊥，所以没有链回 reload —— 干净收工。\\033[0m")

    print("\n\033[1m【4】失败：先恢复，再记录，不传染\033[0m")
    rt2 = Runtime()

    def bad_setup():
        raise RuntimeError("端口被占用")

    rt2.add(Fiber("flaky", frozenset(), bad_setup, lambda: None))
    rt2.provide("x", "flaky")     # 触发 reload（flaky 无依赖，target 非 ⊥）
    f3 = rt2.fibers["flaky"]
    print(f"  一个 setup 必失败的组件：")
    for h in f3.history:
        print(f"    \\033[90m{h}\\033[0m")
    print(f"  最终 state = \\033[36m{f3.state}\\033[0m  error = \\033[31m{f3.error}\\033[0m")
    print("  \\033[90m  失败走的是和正常卸载**同一条** unload 路径（先恢复已构建的累加器），")
    print("  所以失败组件对状态的贡献为零；且错误记录在 fiber 上，")
    print("  兄弟组件照常运行 —— 插件宿主想要的行为。\\033[0m")

    print("\n\033[1m【5】对比 s06/s12：这套状态机的血缘\033[0m")
    print("  INACTIVE → RELOADING → ACTIVE → UNLOADING → INACTIVE")
    print("  · s06 的 turn 结束 = 这里 refresh 判定「目标无变化」")
    print("  · s12 的异步 job = 这里 RELOADING/UNLOADING 的中间态")
    print("  · s20 的先停供 = 这里 UNLOADING 先于一切逆（L-Leave）")
    print("  · s19 的 accumulator = 这里失败路径上的「先恢复」")
    print("  \\033[90m  dsh 的 Algorithm 5 就是这套状态机的工业完整版。\\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章的三句话：")
    print("  1. 生命周期由 target vs committed 的比较驱动，不是由命令驱动")
    print("  2. 惯性：转换跑完才响应新目标；目标又变了就链式切换")
    print("  3. 失败先恢复再记录：贡献为零，不传染兄弟，走同一条 unload 出口")
    print("参考：docs/cordis-paper-spatiotemporal-composability.md §六（论文 §4.2/§4.3）\033[0m")


if __name__ == "__main__":
    demo()
