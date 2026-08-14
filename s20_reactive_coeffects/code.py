#!/usr/bin/env python3
"""s20 — Reactive Coeffects（反应式依赖 + 级联卸载）

    s14 的依赖检查                        s20 的依赖检查
    ┌──────────────────────────┐         ┌──────────────────────────────┐
    │ setup 时 require("db")    │         │ 声明 inject={"db"}            │
    │  → 缺失就抛异常           │         │ 每次上下文变化都重新判定：     │
    │  → 之后没人再管           │         │   activating   依赖刚被满足    │
    └──────────────────────────┘         │   deactivating 依赖刚被撤走    │
                                         │   neutral      与我无关       │
                                         └──────────────────────────────┘

这一章补上 deepseek-harness 最独门、其他教程几乎不讲的两件事：

  1. **依赖满足性在运行时持续重判**，而不是初始化时布线一次。
     依赖出现 → 自动激活；依赖被撤 → 自动停用。

  2. **级联卸载的三段顺序**（Cordis 论文 §4.3.1）：
       provider 先「停供」（依赖者立刻重算）
       → 依赖者先停（级联，自底向上）
       → provider 等依赖者都停了，才撤自己的逆（守卫）
     这个顺序保证了：依赖者永远不会读到「半撤掉的依赖」。

运行：
    python s20_reactive_coeffects/code.py --demo
"""

import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ══════════════════════════════════════════════════════════════════
# s20 新增：Reactive Coeffects
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Change:
    """一次依赖状态变化，对照一个组件的声明分类。

    这个三分类是「反应式」的全部含义（Cordis 论文 Definition 26）：

        activating    σ ⊭ d 且 σ′ ⊧ d   依赖刚被满足 → 激活
        deactivating  σ ⊧ d 且 σ′ ⊭ d   依赖刚被撤走 → 停用
        neutral       其余               与此组件无关

    注意分类**对照规格 d** 而不是对照"某个 key 有没有变"：
    组件只关心"我的依赖集合整体是否从满足翻到不满足"。
    """

    kind: str  # activating | deactivating | neutral


@dataclass
class Component:
    """一个依赖方/提供方。

      inject    声明：我依赖哪些 key（coeffect 规格 d）
      provide   承诺：我会提供哪些 key（provision p）
      setup     效应函数（激活时执行，返回逆）
      status    idle | active | stopping

    单源纪律（single-source）：一个 key 最多一个 provider。
    没有它，下面 target/committed 的对比与级联顺序都无从谈起。
    """

    name: str
    inject: frozenset[str]
    provide: frozenset[str]
    setup: Callable[[], None]
    teardown: Callable[[], None]
    status: str = "idle"

    def satisfied(self, provided: set[str]) -> bool:
        return self.inject <= provided   # 声明的每个 key 都有人提供


class DependencyRuntime:
    """依赖图 + 反应式重判 + 级联卸载。

    这是 s14 的 services dict 的**形式化升级**。
    s14 里 require() 只在 setup 时查一次，之后 provider 被卸载就没人管了；
    这里每一次 provide/unprovide 都触发全局重判。
    """

    def __init__(self) -> None:
        self.components: dict[str, Component] = {}
        self.provided: dict[str, str] = {}     # key → 提供者名字
        self.log: list[tuple[str, str]] = []   # 事件流水（教学用）

    def register(self, comp: Component) -> None:
        if comp.name in self.components:
            raise ValueError(f"组件重名：{comp.name}")
        self.components[comp.name] = comp
        self.log.append(("register", comp.name))

    def activate(self, name: str) -> None:
        """激活一个组件：检查单源纪律 → 跑 setup → 登记它的 provision。

        注意逆在**激活成功之后**由组件自己保存（s19 的可逆效应），
        停用时按 LIFO 撤回。
        """
        comp = self.components[name]
        if comp.status != "idle":
            return
        # 单源纪律：我要提供的 key 已经被别人提供了 → 拒绝激活
        clash = [k for k in comp.provide if k in self.provided]
        if clash:
            raise ValueError(f"{name} 激活失败：{', '.join(clash)} 已被 "
                             f"{', '.join(self.provided[k] for k in clash)} 提供（单源纪律）")
        comp.setup()
        comp.status = "active"
        new_keys = []
        for k in comp.provide:
            if k not in self.provided:
                self.provided[k] = name
                new_keys.append(k)
        self.log.append(("activate", name))
        # 激活即提供：每个新 key 都触发一次重判，
        # 依赖它的组件会在这时自动上线（activating）。
        if new_keys:
            self._reevaluate(set(new_keys))

    # ── 反应式重判：变化的传播 ─────────────────────────────────────
    def _reevaluate(self, changed_keys: set[str]) -> None:
        """对照每个组件的声明，把这次变化分类，并驱动激活/停用。

        只对「依赖声明里包含变化 key」的组件分类（其余是 neutral，
        根本不用算）——这是 O(依赖者) 而不是 O(全部组件)。
        """
        for name, comp in list(self.components.items()):
            if not (set(comp.inject) & changed_keys):
                continue
            is_ = comp.satisfied(set(self.provided))
            was = comp.status == "active" and not is_
            # 更精确的"变前"本应从变前快照判定；教学版用
            # status 判断方向：空闲+现在满足=activating，
            # 活跃+现在不满足=deactivating，其余 neutral。
            change = (Change("activating") if comp.status == "idle" and is_
                      else Change("deactivating") if was
                      else Change("neutral"))
            self._apply(name, comp, change)

    def _apply(self, name: str, comp: Component, change: Change) -> None:
        if change.kind == "activating" and comp.status == "idle":
            self.activate(name)
        elif change.kind == "deactivating" and comp.status == "active":
            # 停用同样走三段：先停供、再通知依赖者、最后撤逆
            self._stop(name, reason="依赖被撤走")

    def provide(self, name: str, key: str) -> None:
        self.provided[key] = name
        self.log.append(("provide", f"{name} → {key}"))
        self._reevaluate({key})

    def unprovide(self, name: str, key: str) -> None:
        """撤走一个绑定 —— 注意这**不是**卸载，是「停供」。

        停供和卸载分离是级联卸载的第一段（L-Leave）：
        provider 进入 stopping 状态，立刻从满足判定里消失，
        但它的效应**还没撤**——依赖者先看到了"不可满足"。
        """
        if self.provided.get(key) == name:
            del self.provided[key]
            self.log.append(("unprovide", f"{name} → {key}"))
            self._reevaluate({key})

    # ── 级联卸载：三段顺序 ────────────────────────────────────────
    def unload(self, name: str) -> None:
        """卸载组件。三段顺序缺一不可：

          ① 停供   先把自己提供的 key 全部撤下。
                    依赖者立刻重算 → 不可满足 → 开始停用（级联）。
          ② 守卫   等所有依赖者（含间接依赖）都停完。
                    依赖者的停用会级联它自己的依赖者 —— 自底向上。
          ③ 撤逆   最后才执行自己的逆（LIFO）。
        """
        comp = self.components[name]
        if comp.status == "idle":
            return

        # ① 停供（先于一切逆）。先记下"有谁在等我"，
        #    守卫的语义是：这些依赖者（含间接）全部停完，我才撤逆。
        dependents = [c for c in self.components.values()
                      if c.status == "active"
                      and any(k in c.inject for k in comp.provide)]
        for dep in dependents:
            self.log.append(("guard", f"{name} 在等 {dep.name} 停用"))
        for k in list(comp.provide):
            self.unprovide(name, k)
        # ② 守卫：真实系统这里是 await（依赖者的停用是异步转换）；
        #    教学版级联是同步递归的，unprovide 返回时依赖者已停完。
        #    无论同步异步，顺序不变：依赖者的逆先于 provider 的逆。
        # ③ 撤逆
        comp.status = "stopping"
        comp.teardown()
        comp.status = "idle"
        self.log.append(("unload", name))

    def _stop(self, name: str, reason: str) -> None:
        """依赖者停用：同样是 停供 → 级联 → 撤逆。"""
        self.log.append(("stopping", f"{name}（{reason}）"))
        self.unload(name)


# ══════════════════════════════════════════════════════════════════
# 演示
# ══════════════════════════════════════════════════════════════════


def demo() -> None:
    rt = DependencyRuntime()
    print("\033[1m【1】依赖声明 + 单源纪律\033[0m")

    def mk(name, inject=(), provide=()):
        def setup():
            print(f"      ▲ {name} 上线（setup 执行）")
        def teardown():
            print(f"      ▼ {name} 下线（逆执行）")
        rt.register(Component(name, frozenset(inject), frozenset(provide), setup, teardown))

    mk("db", provide={"db"})
    mk("cache", inject={"db"}, provide={"cache"})
    mk("app", inject={"db", "cache"})
    mk("db2", provide={"db"})      # 故意违反单源纪律的第二个 db

    print("  db     提供 {db}")
    print("  cache  依赖 {db}，提供 {cache}")
    print("  app    依赖 {db, cache}")
    print("  db2    也想提供 {db}（违反单源纪律）")

    print("\n\033[1m【2】反应式激活：提供 db，依赖者自动上线\033[0m")
    print("  激活 db（会提供 key db，并触发全局重判）：")
    rt.activate("db")
    print("  重判后的状态：")
    for c in rt.components.values():
        print(f"    {c.name:<6} status=\033[36m{c.status:<6}\033[0m satisfied={c.satisfied(set(rt.provided))}")
    print("  \033[90m  cache 和 app 没有调用任何「启动」代码 —— 是分类器激活的。")
    print("  对照 s14：require() 缺失时直接 raise —— 那只能表达静态依赖。\033[0m")

    print("\n\033[1m【3】单源纪律执行：第二个 db 想上线\033[0m")
    try:
        rt.activate("db2")
    except ValueError as e:
        print(f"  \033[31m{e}\033[0m")
    print("  \033[90m  没有这条纪律，「谁在提供 db」就回答不了，级联顺序也无从谈起。\033[0m")

    print("\n\033[1m【4】级联卸载：三段顺序\033[0m")
    print("  卸载 db（cache 和 app 都依赖它）：")
    rt.unload("db")
    print("\n  事件流水（顺序就是纪律）：")
    for kind, msg in rt.log:
        mark = {"unprovide": "①停供", "stopping": "②级联停用",
                "unload": "③撤逆", "guard": "②守卫"}.get(kind, "")
        if kind in ("unprovide", "stopping", "unload", "guard"):
            print(f"    \033[36m{msg:<36}\033[0m \033[90m{mark}\033[0m")
    print("  \033[90m  注意顺序：cache 和 app 的逆都撤完之后，db 的逆才执行。")
    print("  依赖者永远读不到「半撤掉的依赖」—— Cordis 论文定理 63 的工程形态。\033[0m")

    print("\n\033[1m【5】依赖回来，组件自动复活\033[0m")
    print("  重新激活 db：")
    rt.activate("db")
    for c in rt.components.values():
        print(f"    {c.name:<6} status=\033[36m{c.status}\033[0m")
    print("  \033[90m  没有人手动重启 cache/app —— 是提供 db 的动作触发了重判。")
    print("  分类对照的是**声明**，不是「key 变了没变」—— 无关组件不被打扰。\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章的三句话：")
    print("  1. 依赖满足性是运行时属性，每次上下文变化都对照声明重新判定")
    print("  2. 分类只有三种：activating / deactivating / neutral")
    print("  3. 级联卸载三段：先停供 → 依赖者先停（守卫）→ 最后撤逆")
    print("参考：docs/cordis-paper-spatiotemporal-composability.md §四、§六（论文 §3.2/§4.3）\033[0m")




if __name__ == "__main__":
    demo()
