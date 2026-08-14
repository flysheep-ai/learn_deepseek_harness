"""每一章的 demo 都必须能离线跑通。

这是本课程最重要的测试：每一章都是一份完整、可运行的实现，
任何一章坏掉都意味着某个机制被改坏了。
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHAPTERS = [
    "s01_agent_loop", "s02_tool_use", "s03_tool_registry", "s04_permission",
    "s05_session_event_log", "s06_turn_and_step", "s07_prompt_assembly",
    "s08_skill_loading", "s09_subagent", "s10_context_compaction",
    "s11_task_system", "s12_background_jobs", "s13_event_bus",
    "s14_plugin_system", "s15_capability_seams", "s16_agent_team",
    "s17_goal_loop", "s18_full_harness",
    "s19_revertible_effects", "s20_reactive_coeffects",
    "s21_inertial_lifecycle", "s22_session_lifecycle",
]


class ChapterDemoTest(unittest.TestCase):
    def test_demo_runs_offline(self):
        for ch in CHAPTERS:
            with self.subTest(chapter=ch):
                r = subprocess.run(
                    [sys.executable, "code.py", "--demo"],
                    cwd=ROOT / ch, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(r.returncode, 0,
                                 f"{ch} --demo 失败：\n{r.stdout[-1500:]}\n{r.stderr[-800:]}")

    def test_debug_flag_works(self):
        # 抽查几章：--debug 不应改变行为，只增加 trace 输出
        for ch in ("s06_turn_and_step", "s10_context_compaction", "s18_full_harness"):
            with self.subTest(chapter=ch, flag="--debug"):
                r = subprocess.run(
                    [sys.executable, "code.py", "--demo", "--debug"],
                    cwd=ROOT / ch, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(r.returncode, 0, f"{ch} --demo --debug 失败：{r.stderr[-800:]}")
                self.assertIn("model request", r.stdout + r.stderr,
                              f"{ch} --debug 没有 trace 输出")


if __name__ == "__main__":
    unittest.main()
