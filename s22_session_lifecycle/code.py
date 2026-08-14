#!/usr/bin/env python3
"""s22 — Session Lifecycle（会话生命周期：种子、分叉、激活权限）

    进程重启 / fork / 恢复之后，四个 s05 没回答的问题：

    1. 恢复出来的会话和原生会话在日志上**逐字节相同** ——
       怎么区分"上个生命周期遗留的"和"本次新产生的"？
       → session/end-seed：种子边界事件（dsh session.md）

    2. fork 一个会话，子会话从哪里开始？种子怎么标？
       → fork(source, upto) 的一行实现，终于补上了

    3. s17 的 goal 说"active 就自动继续" —— 但恢复会话后，
       没有人看着的自动续跑是安全风险。
       → activation：armed / disarmed（dsh goal.md）

    4. 派生数据（messages、goal 状态）每次都重算太慢，想缓存？
       → 派生缓存 + 失效，而不是第二份真相

运行：
    python s22_session_lifecycle/code.py --demo
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ══════════════════════════════════════════════════════════════════
# 沿用 s05（最小版）：Session / SessionEvent
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SessionEvent:
    seq: int
    type: str
    data: dict[str, Any]
    time: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"seq": self.seq, "type": self.type, "time": self.time, "data": self.data},
                          ensure_ascii=False)

    @staticmethod
    def from_json(line: str) -> "SessionEvent":
        d = json.loads(line)
        return SessionEvent(d["seq"], d["type"], d["data"], d.get("time", 0.0))


EV_USER_MESSAGE = "user/message"
EV_ASSISTANT_MESSAGE = "assistant/message"
EV_COMPACTION_START = "compaction/start"
EV_COMPACTION_END = "compaction/end"
EV_GOAL_START = "goal/start"
EV_GOAL_COMPLETE = "goal/complete"

# ── s22 新增 ──────────────────────────────────────────────────────
#
# 种子边界：标记"这条之前的都是种子（恢复/fork 来的），
# 本次生命周期一条都没产生过"。
#
# 为什么需要它？恢复出来的会话和原生会话在日志上**逐字节相同**。
# 有些配对检查需要知道"这段历史是不是我亲手写的"：
#   例如一个未闭合的 compaction/start ——
#   如果是上个生命周期崩溃留下的，它是**陈旧证据**，应当忽略；
#   如果是本次留下的，它是**进行中的锁**，必须阻塞新入口。
EV_SESSION_END_SEED = "session/end-seed"

SURFACE_EVENTS = {EV_USER_MESSAGE, EV_ASSISTANT_MESSAGE}


class Session:
    def __init__(self, session_id: str | None = None, path: Path | None = None) -> None:
        self.id = session_id or f"ses_{uuid.uuid4().hex[:8]}"
        self.path = path
        self._events: list[SessionEvent] = []
        self._seq = 0

    def append(self, type_: str, data: dict[str, Any]) -> SessionEvent:
        json.dumps(data, ensure_ascii=False)
        self._seq += 1
        ev = SessionEvent(self._seq, type_, data)
        self._events.append(ev)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(ev.to_json() + "\n")
        return ev

    def events(self) -> list[SessionEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def first_live_seq(self) -> int:
        """第一条"本生命周期产生"的事件序号。

        找**最后一条** end-seed：它之后的就是本次生命周期的。
        种子已经以 end-seed 结尾时不再重复打标 —— 否则每次
        重新打开都凭空长一条事件。
        """
        mark = 0
        for ev in self._events:
            if ev.type == EV_SESSION_END_SEED:
                mark = ev.seq
        return mark + 1

    def live_events(self) -> list[SessionEvent]:
        return [e for e in self._events if e.seq >= self.first_live_seq()]

    @classmethod
    def load(cls, path: Path) -> "Session":
        s = cls(session_id=path.stem, path=path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ev = SessionEvent.from_json(line)
                s._events.append(ev)
                s._seq = max(s._seq, ev.seq)
        return s

    def fork(self, upto: int | None = None) -> "Session":
        """分叉：复制前 N 条事件到一个新会话。

        s05 的「自己动手改」第 5 题，现在正式补上。
        因为真相是**不可变的事件流**，分叉天然就是"复制 + 打标"：

           · 复制的事件 = 新会话的种子
           · 追加一条 end-seed 标记边界
           · 新会话从边界之后开始写自己的历史

        两个会话从此互不影响 —— 这正是"真相是投影的来源"
        才可能有的操作。如果真相是可变的 messages 列表，
        你得深拷贝，还要担心里面的引用共享。
        """
        n = upto if upto is not None else self._seq
        child = Session(session_id=f"{self.id}_fork")
        for ev in self._events[:n]:
            child._events.append(ev)
        child._seq = max((e.seq for e in child._events), default=0)
        child.append(EV_SESSION_END_SEED, {"forked_from": self.id, "at_seq": n})
        return child


def find_unclosed_compaction(session: Session) -> list[SessionEvent]:
    """找出没有配对 end 的 compaction/start。

    s05 说过：一条没有配对的 tool/call 是崩溃铁证。
    compaction/start 同理 —— 它是记录在日志里的**锁**。

    但恢复之后，上个生命周期的孤儿锁是**陈旧证据**：
    忽略它，让本次生命周期正常开工。
    本生命周期的孤儿锁才是真正的"压缩进行中"，要阻塞。
    """
    unclosed: list[SessionEvent] = []
    for ev in session.events():
        if ev.type == EV_COMPACTION_START:
            unclosed.append(ev)
        elif ev.type == EV_COMPACTION_END and unclosed:
            unclosed.pop()
    return unclosed


# ══════════════════════════════════════════════════════════════════
# s22 新增：Goal Activation（armed / disarmed）
# ══════════════════════════════════════════════════════════════════


class GoalActivation:
    """s17 的 Goal 把「状态」和「权限」混在了 status 里。

    dsh 把它们分成两层（goal.md）：

        durable phase      active / paused / blocked / complete
                           —— 持久状态，写进日志，恢复后还在
        activation         armed / disarmed
                           —— **进程级**权限，恢复后必须重新授权

    为什么分开？因为「目标还没完成」是一个持久事实，
    而「允许在没有人看着的时候自动续跑」是一个安全决定。
    恢复会话后 automation 不该自己跑起来 —— 必须等一个人
    重新 armed。
    """

    def __init__(self) -> None:
        self.armed = False

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def may_continue(self) -> bool:
        return self.armed


# ══════════════════════════════════════════════════════════════════
# s22 新增：派生缓存 —— 带失效的缓存，不是第二份真相
# ══════════════════════════════════════════════════════════════════


class DerivedCache:
    """缓存一个从事件日志派生的值。

    s05 说「每步重新 derive」保证了不变量；s11 说「不缓存任务清单」。
    但如果派生真的贵（比如投影出 10 万条消息），缓存是合理优化 ——
    前提是它**带失效机制**：

        · 记录缓存时日志的长度
        · 日志变长 = 缓存作废，下次重算

    这是「派生缓存」和「第二份真相」的区别：
    第二份真相允许有人直接往里写；派生缓存只能被重新计算填满，
    而且任何日志写入都会让它作废。
    """

    def __init__(self, derive) -> None:
        self._derive = derive
        self._cached: Any = None
        self._cached_at_len: int = -1

    def get(self, session: Session) -> Any:
        if self._cached_at_len != len(session):
            # 日志变了 → 作废重算
            self._cached = self._derive(session)
            self._cached_at_len = len(session)
        return self._cached


# ══════════════════════════════════════════════════════════════════
# 演示
# ══════════════════════════════════════════════════════════════════


def demo() -> None:
    import tempfile

    print("\033[1m【1】崩溃现场：一条没有配对的 compaction/start\033[0m")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        s = Session(path=p)
        s.append(EV_USER_MESSAGE, {"content": "帮我重构这个模块"})
        s.append(EV_COMPACTION_START, {"boundary": 1})
        # …… 这里进程崩溃了，end 永远没写
        print(f"  磁盘上有 {len(s)} 条事件，compaction/start 没有配对的 end。")
        print("  这就是「压缩进行到一半挂了」的铁证 —— s10 说过括号结构的意义。")

        print("\n\033[1m【2】恢复会话：怎么区分陈旧证据和进行中的锁？\033[0m")
        restored = Session.load(p)
        live_seq = restored.first_live_seq()
        print(f"  first_live_seq = {live_seq} —— 恢复出来的会话没有 end-seed，")
        print("  所以全部事件都被当作「本生命周期」的：")
        unclosed = find_unclosed_compaction(restored)
        for ev in unclosed:
            in_seed = ev.seq < live_seq
            tag = ("\033[33m种子中 → 陈旧证据，忽略\033[0m" if in_seed
                   else "\033[31m本次 → 进行中的锁，阻塞！\033[0m")
            print(f"    \033[90m#{ev.seq} 未闭合\033[0m  " + tag)
        print("  \033[90m  没有 end-seed 时，恢复后的孤儿锁会被误判成进行中的锁 ——")
        print("  阻塞一切新压缩，死锁。这就是 end-seed 存在的理由。\033[0m")

        print("\n\033[1m【3】正式恢复流程：打标 → 陈旧证据被正确忽略\033[0m")
        restored.append(EV_SESSION_END_SEED, {})
        print(f"  打标后 first_live_seq = {restored.first_live_seq()}")
        print("  孤儿锁位于种子区 → 陈旧证据，忽略；新生命周期照常开工：")
        restored.append(EV_USER_MESSAGE, {"content": "继续上次的重构"})
        unclosed2 = find_unclosed_compaction(restored)
        stale = [e for e in unclosed2 if e.seq < restored.first_live_seq()]
        print(f"    未闭合 {len(unclosed2)} 条，其中陈旧 {len(stale)} 条 → 不阻塞")
        print("  \033[90m  这就是 dsh 里 session/end-seed 的用途之一。\033[0m")

    print("\n\033[1m【4】fork：正式实现（s05 留下的作业）\033[0m")
    parent = Session()
    for i in range(5):
        parent.append(EV_USER_MESSAGE, {"content": f"消息 {i}"})
    child = parent.fork(upto=3)
    print(f"  父：{len(parent)} 条   子：{len(child)} 条（前 3 条 + 1 条 end-seed）")
    print(f"  子的 first_live_seq = {child.first_live_seq()} —— 前 3 条是种子")
    child.append(EV_USER_MESSAGE, {"content": "子会话自己的消息"})
    parent.append(EV_USER_MESSAGE, {"content": "父会话自己的消息"})
    print(f"  两边各自续写：父 {len(parent)} 条 / 子 {len(child)} 条 —— 互不影响")

    print("\n\033[1m【5】goal activation：armed / disarmed\033[0m")
    sess = Session()
    store_goal = sess.append(EV_GOAL_START, {"statement": "修好除零 bug", "status": "active",
                                             "round": 2, "max_rounds": 5})
    act = GoalActivation()
    print("  目标状态：active（持久，来自日志）  round=2/5")
    state_word = "\033[31mdisarmed\033[0m" if not act.may_continue() else "\033[32marmed\033[0m"
    print(f"  activation：{state_word}  —— 进程级，重启即失效")
    print("  \033[90m  恢复会话后：目标还 active，但没人重新 arm 之前，")
    print("  自动续跑不会发生 —— 这就是「安全决定」和「持久事实」的分层。\033[0m")
    act.arm()
    print("  人重新 arm 之后：\033[32marmed\033[0m → turn 结束的评估循环恢复工作")

    print("\n\033[1m【6】派生缓存：快，但不是第二份真相\033[0m")
    s2 = Session()
    cache = DerivedCache(lambda sess: f"{len(sess)} 条事件的派生值")
    print(f"  第一次取：{cache.get(s2)}（重算）")
    print(f"  第二次取：{cache.get(s2)}（缓存命中，没有重算）")
    s2.append(EV_USER_MESSAGE, {"content": "新事件"})
    print(f"  追加一条后：{cache.get(s2)}（日志变长 → 自动失效重算）")
    print("  \033[90m  区别：派生缓存只能被重新计算填满；第二份真相允许直接写入。")
    print("  前者坏不了不变量，后者迟早坏。\033[0m")

    print("\n\033[90m" + "─" * 66)
    print("这一章的三句话：")
    print("  1. end-seed 给恢复/fork 的历史画一条「种子边界」—— 没有它，")
    print("     陈旧证据（孤儿锁）和进行中的锁无法区分")
    print("  2. fork = 复制事件 + 打标；真相是不可变事件流，分叉才是免费操作")
    print("  3. 持久状态（goal phase）与进程权限（activation）是两层 ——")
    print("     恢复后自动续跑必须重新授权")
    print("参考：docs/cordis-paper-spatiotemporal-composability.md；dsh session.md / goal.md\033[0m")


if __name__ == "__main__":
    demo()
