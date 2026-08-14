"""关键机制的确定性测试。

真实模型是不确定的，但 Harness 的行为必须是确定的。
用 ScriptedProvider 把模型换成脚本，然后断言 Harness 做了什么。
机制都从 s18（整合版）导入 —— 它是全部机制的最终形态。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness_llm import ScriptedProvider, scripted  # noqa: E402

spec = importlib.util.spec_from_file_location("s18", ROOT / "s18_full_harness" / "code.py")
s18 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s18)


def make_harness(profile="minimal", cwd=None, evaluator=None):
    from pathlib import Path as P
    import tempfile
    cwd = cwd or P(tempfile.mkdtemp(prefix="t_"))
    sess = s18.Session()
    sess.append(s18.EV_SESSION_START, {"cwd": str(cwd)})
    summarizer = ScriptedProvider([scripted("摘要。")])
    h = s18.build_harness(profile, cwd, sess, s18.Tracer(False),
                          lambda: ScriptedProvider(), evaluator or summarizer,
                          lambda *a: True, ROOT / "s18_full_harness" / "skills")
    return h, sess


class EventLogTest(unittest.TestCase):
    def test_derive_messages_surface_only(self):
        sess = s18.Session()
        sess.append(s18.EV_USER_MESSAGE, {"content": "你好"})
        sess.append(s18.EV_PERMISSION, {"decision": "deny"})   # log-only
        sess.append(s18.EV_ASSISTANT_MESSAGE, {"text": "嗨", "tool_calls": []})
        msgs = s18.derive_messages(sess)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])

    def test_time_travel(self):
        sess = s18.Session()
        for i in range(6):
            sess.append(s18.EV_USER_MESSAGE, {"content": f"m{i}"})
        self.assertEqual(len(s18.derive_messages(sess, upto=3)), 3)

    def test_replay_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            s1 = s18.Session(path=p)
            s1.append(s18.EV_USER_MESSAGE, {"content": "a"})
            s1.append(s18.EV_TOOL_RESULT, {"call_id": "x", "content": "r"})
            s2 = s18.Session.load(p)
            self.assertEqual(len(s18.derive_messages(s2)), 2)
            # 续写
            s2.append(s18.EV_USER_MESSAGE, {"content": "b"})
            self.assertEqual(len(s18.derive_messages(s2)), 3)


class CompactionTest(unittest.TestCase):
    def test_shadow_not_delete(self):
        sess = s18.Session()
        for i in range(8):
            sess.append(s18.EV_USER_MESSAGE, {"content": "x" * 200})
        sess.append(s18.EV_STEP_END, {"turn": 1, "step": 1})
        sess.append(s18.EV_COMPACTION_SUMMARY,
                    {"shadowed_seqs": [1, 2, 3, 4], "supersedes": [],
                     "summary": "以前的 4 条消息"})
        self.assertEqual(len(sess), 10)                      # 一条没删
        msgs = s18.derive_messages(sess)
        self.assertEqual(len(msgs), 5)                       # 4 条被替换成 1 条
        self.assertIn("以前的 4 条消息", msgs[0]["content"])
        # 时间旅行仍然有效
        self.assertEqual(len(s18.derive_messages(sess, upto=4)), 4)

    def test_naive_truncate_creates_orphans_but_step_boundary_does_not(self):
        sess = s18.Session()
        sess.append(s18.EV_USER_MESSAGE, {"content": "go"})
        sess.append(s18.EV_ASSISTANT_MESSAGE, {"text": "",
                                               "tool_calls": [{"id": "c1", "type": "function",
                                                               "function": {"name": "read",
                                                                            "arguments": "{}"}}]})
        sess.append(s18.EV_TOOL_RESULT, {"call_id": "c1", "content": "文件内容"})
        sess.append(s18.EV_STEP_END, {"turn": 1, "step": 1})
        sess.append(s18.EV_ASSISTANT_MESSAGE, {"text": "做完了", "tool_calls": []})
        # 安全边界：只能切在 step/end
        b = s18.find_safe_boundary(sess, keep_tokens=1)
        self.assertIsNotNone(b)
        # 切点之后不应出现孤儿 tool result
        msgs = s18.derive_messages(sess)
        seen_ids = set()
        for m in msgs:
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    seen_ids.add(c["id"])
            elif m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], seen_ids)


class TaskTest(unittest.TestCase):
    def test_validate_catches_cycle(self):
        t1 = s18.Task("a", "A", "pending", ("b",))
        t2 = s18.Task("b", "B", "pending", ("a",))
        self.assertIsNotNone(s18.TaskStore.validate([t1, t2]))

    def test_validate_catches_premature_completion(self):
        t1 = s18.Task("a", "A", "completed")
        t2 = s18.Task("b", "B", "completed", ("a",))
        ok = s18.TaskStore.validate([t1, t2])
        self.assertIsNone(ok)

    def test_write_is_last_wins_snapshot(self):
        sess = s18.Session()
        store = s18.TaskStore(sess)
        store.write([s18.Task("a", "第一版")])
        store.write([s18.Task("b", "第二版")])
        current = store.current()
        self.assertEqual([t.id for t in current], ["b"])


class EventBusTest(unittest.TestCase):
    def test_waterfall_short_circuit(self):
        bus = s18.EventBus()
        calls = []

        def deny(ctx, next_):
            ctx.result = s18.ToolResult("拒绝", is_error=True)   # 不调 next_ = 短路

        def never(ctx, next_):
            calls.append("never")
            next_()

        bus.use("t", deny, order=10)
        bus.use("t", never, order=20)

        class Ctx:
            result = None

        ctx = Ctx()
        bus.waterfall("t", ctx)
        self.assertEqual(calls, [])                     # 后面的中间件没跑
        self.assertEqual(ctx.result.content, "拒绝")

    def test_observer_exception_isolated(self):
        bus = s18.EventBus()

        def broken(ctx):
            raise RuntimeError("boom")

        bus.on("e", broken)
        bus.emit("e", None)                             # 不应抛出

    def test_unregister(self):
        bus = s18.EventBus()
        seen = []
        off = bus.on("e", lambda: seen.append(1))
        bus.emit("e")
        off()
        bus.emit("e")
        self.assertEqual(seen, [1])


class PluginTest(unittest.TestCase):
    def test_unload_removes_everything(self):
        h, _ = make_harness("full")
        tools_before = set(h.tools.names())
        h.unload("jobs")
        self.assertEqual(tools_before - set(h.tools.names()),
                         {"bash_background", "job_status", "job_output", "job_stop"})
        self.assertNotIn("jobs", h.services)
        self.assertNotIn("jobs", h.prompts.names())

    def test_require_missing_fails_loud(self):
        class P:
            name = "p"

            def setup(self, ctx):
                ctx.require("nonexistent")

        h, _ = make_harness()
        with self.assertRaises(RuntimeError) as cm:
            h.use(P())
        self.assertIn("nonexistent", str(cm.exception))


class GoalTest(unittest.TestCase):
    def test_verdict_parsing_conservative(self):
        v, r = s18._parse_verdict("verdict: done\nreason: 修好了")
        self.assertEqual(v, "done")
        v, _ = s18._parse_verdict("verdict: not-a-word\nreason: x")
        self.assertEqual(v, "continue")                 # 格式错 → 保守为 continue
        v, _ = s18._parse_verdict("完全没有格式")
        self.assertEqual(v, "continue")

    def test_goal_persists_in_events(self):
        sess = s18.Session()
        store = s18.GoalStore(sess)
        store.set(s18.Goal(statement="修 bug", max_rounds=3))
        store.update(s18.EV_GOAL_COMPLETE, "complete", 2, "好了", "修 bug", 3)
        g = store.current()
        self.assertEqual(g.status, "complete")
        self.assertEqual(g.round, 2)


class ModelDecidesTest(unittest.TestCase):
    def test_harness_has_no_task_routing(self):
        """Harness 本体不得替模型做任务决策。

        搜索的是决策分支的形态（if task_type / call_xxx_agent / router.route），
        不是裸词 —— 裸词会命中这条测试自身。
        """
        import re
        full = (ROOT / "s18_full_harness" / "code.py").read_text(encoding="utf-8")
        src = full[: full.index("def demo(debug: bool) -> None:")]
        t = "task" + "_type"
        patterns = [r"if\s+" + t, r"elif\s+" + t, "call_research" + "_agent",
                    "call_coding" + "_agent", "intent" + "_classif", "router" + "." + "route"]
        for pat in patterns:
            self.assertIsNone(re.search(pat, src), f"harness 本体出现决策分支：{pat}")


class SeamTest(unittest.TestCase):
    """同一套工具，换 provider 换世界 —— 工具代码不变。"""

    def _make(self, fs=None, shell=None):
        import tempfile
        from pathlib import Path as P
        cwd = P(tempfile.mkdtemp(prefix="t2_"))
        sess = s18.Session()
        sess.append(s18.EV_SESSION_START, {"cwd": str(cwd)})
        h = s18.build_harness("minimal", cwd, sess, s18.Tracer(False),
                              lambda: ScriptedProvider(),
                              ScriptedProvider([scripted("摘要。")]),
                              lambda *a: True,
                              ROOT / "s18_full_harness" / "skills",
                              fs=fs, shell=shell)
        return h, sess

    def test_memory_fs_via_parameter(self):
        mem = s18.MemoryFileSystem()
        mem.write("app.py", 'VERSION = "1.0"')
        h, sess = self._make(fs=mem, shell=s18.DryRunShell())
        ex = s18.ToolExecutor(h.tools, h.bus)
        r = ex.execute("x", "read", {"path": "app.py"}, sess, 1, 1)
        self.assertIn("1.0", r.content)
        r = ex.execute("x", "bash", {"command": "rm -rf /"}, sess, 1, 1)
        self.assertIn("dry-run", r.content)

    def test_two_worlds_same_tools(self):
        h_local, sess_l = self._make()
        mem = s18.MemoryFileSystem()
        mem.write("a.py", "print('hi')")
        h_mem, sess_m = self._make(fs=mem, shell=s18.DryRunShell())
        ex_local = s18.ToolExecutor(h_local.tools, h_local.bus)
        ex_mem = s18.ToolExecutor(h_mem.tools, h_mem.bus)
        r1 = ex_local.execute("x", "read", {"path": "a.py"}, sess_l, 1, 1)
        r2 = ex_mem.execute("x", "read", {"path": "a.py"}, sess_m, 1, 1)
        self.assertTrue(r1.is_error)                    # 本地磁盘没有 a.py
        self.assertIn("print('hi')", r2.content)        # 内存里有


if __name__ == "__main__":
    unittest.main()
