"""시스템 프롬프트가 같은 날 안에서 바뀌지 않아야 Gemini 자동 캐시가 실행 사이에 재사용된다(11/6)."""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.modules.setdefault("oracledb", types.SimpleNamespace(connect=lambda **_: None))

from agent_runtime.prompt.builder import build_system_prompt, current_time_context

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]


class PromptCacheStabilityTest(unittest.TestCase):
    def test_same_day_different_minutes_same_prompt(self):
        a = build_system_prompt(tools=[], repo_root=ROOT, now=datetime(2026, 11, 6, 9, 1, tzinfo=KST))
        b = build_system_prompt(tools=[], repo_root=ROOT, now=datetime(2026, 11, 6, 15, 47, tzinfo=KST))
        self.assertEqual(a, b)
        self.assertIn("Today: 2026-11-06 KST", a)

    def test_time_goes_to_dynamic_context(self):
        self.assertEqual(current_time_context(datetime(2026, 11, 6, 15, 47, tzinfo=KST)), "Current time: 2026-11-06 15:47 KST.")


if __name__ == "__main__":
    unittest.main()
