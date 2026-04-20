import unittest
from datetime import datetime, timezone

from agent_runtime.tool.forward_test.create_forward_test import (
    CreateForwardTestAction,
    _build_job_question,
    _build_schedules,
    _parse_next_run_without_croniter,
)


class ForwardTestScheduleTests(unittest.TestCase):
    def test_hour_lists_support_two_intraday_runs(self) -> None:
        self.assertEqual(
            _parse_next_run_without_croniter(
                "0 2,6 * * 1-5",
                datetime(2026, 4, 20, 2, 5, tzinfo=timezone.utc),
            ).isoformat(),
            "2026-04-20T06:00:00+00:00",
        )

        self.assertEqual(
            _parse_next_run_without_croniter(
                "0 2,6 * * 1-5",
                datetime(2026, 4, 20, 6, 5, tzinfo=timezone.utc),
            ).isoformat(),
            "2026-04-21T02:00:00+00:00",
        )

    def test_weekday_ranges_skip_weekends(self) -> None:
        self.assertEqual(
            _parse_next_run_without_croniter(
                "0 2,6 * * 1-5",
                datetime(2026, 4, 17, 6, 5, tzinfo=timezone.utc),
            ).isoformat(),
            "2026-04-20T02:00:00+00:00",
        )

    def test_multi_schedule_action_keeps_roles_and_crons(self) -> None:
        action = CreateForwardTestAction(
            name="뉴스 데이트레이딩",
            strategy_type="llm",
            strategy_prompt="오전 뉴스 기반 전략",
            schedules=[
                {"role": "buy", "cron_expression": "0 2 * * 1-5", "prompt": "3종목 매수"},
                {"role": "sell", "cron_expression": "0 6 * * 1-5", "prompt": "전량 매도"},
            ],
        )

        self.assertEqual(
            _build_schedules(action),
            [
                {"role": "buy", "cron_expression": "0 2 * * 1-5", "prompt": "3종목 매수"},
                {"role": "sell", "cron_expression": "0 6 * * 1-5", "prompt": "전량 매도"},
            ],
        )

    def test_extra_schedule_prompt_can_reference_same_forward_test(self) -> None:
        action = CreateForwardTestAction(
            name="뉴스 데이트레이딩",
            strategy_type="llm",
            strategy_prompt="오전 뉴스 기반 전략",
        )

        question = _build_job_question(
            action,
            {"role": "sell", "cron_expression": "0 6 * * 1-5", "prompt": "전량 매도"},
            "ft-1",
        )

        self.assertIn("forward_test_id: ft-1", question)
        self.assertIn("schedule_role: sell", question)
        self.assertIn("전량 매도", question)


if __name__ == "__main__":
    unittest.main()
