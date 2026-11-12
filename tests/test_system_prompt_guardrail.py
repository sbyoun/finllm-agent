"""투자 자문 금지 규칙이 시스템 프롬프트 맨 앞에 남아 있는지 — 4/17 다른 수정에 묻혀 약해진 적이 있다(11/6 복구)."""
import unittest

from agent_runtime.prompt import load_system_prompt


class AdviceGuardrailTest(unittest.TestCase):
    def setUp(self):
        self.prompt = load_system_prompt()

    def test_guardrail_section_comes_first(self):
        idx = self.prompt.find("## 투자 자문 금지")
        self.assertGreater(idx, -1, "투자 자문 금지 절이 없다")
        self.assertLess(idx, self.prompt.find("## Rules"), "투자 자문 금지 절은 다른 규칙보다 앞에 둔다")

    def test_guardrail_covers_observed_patterns(self):
        # 11/4–11/5 운영에서 실제로 나온 위반 유형
        for phrase in ["너의 선택은", "일정표", "전량 매도", "체결 확률", "확인하지 못했다", "최종 투자 판단과 책임은 이용자 본인에게", "거절 문장을 쓰지 않는다", "가격대를 숫자로"]:
            self.assertIn(phrase, self.prompt, phrase)


if __name__ == "__main__":
    unittest.main()
