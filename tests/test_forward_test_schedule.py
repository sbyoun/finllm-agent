import unittest
from datetime import datetime, timezone

from agent_runtime.tool.forward_test.create_forward_test import _parse_next_run_without_croniter


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


if __name__ == "__main__":
    unittest.main()
