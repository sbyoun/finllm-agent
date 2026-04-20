import unittest
from unittest.mock import patch

from agent_runtime.tool.forward_test.save_forward_snapshot import (
    _compute_return_pct,
    _compute_total_value,
    _refresh_holding_prices,
)


class SaveForwardSnapshotTests(unittest.TestCase):
    def test_total_value_uses_current_price_and_cash(self) -> None:
        total = _compute_total_value(
            [{"symbol": "AAA", "qty": 3, "avg_cost": 1000, "current_price": 1200}],
            500,
        )

        self.assertEqual(total, 4100)

    def test_total_value_falls_back_to_average_cost_when_mark_price_missing(self) -> None:
        total = _compute_total_value(
            [{"symbol": "AAA", "shares": 2, "avg_cost": 1500}],
            1000,
        )

        self.assertEqual(total, 4000)

    def test_return_pct_uses_initial_capital(self) -> None:
        self.assertEqual(_compute_return_pct(11000, 10000), 10)

    def test_return_pct_falls_back_without_initial_capital(self) -> None:
        self.assertEqual(_compute_return_pct(11000, 0, fallback=3.5), 3.5)

    def test_refresh_holding_prices_uses_latest_close_when_available(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.save_forward_snapshot._fetch_latest_close",
            return_value=2500,
        ):
            holdings = _refresh_holding_prices(
                [{"symbol": "005930", "qty": 1, "avg_cost": 2000, "current_price": 2000}]
            )

        self.assertEqual(holdings[0]["current_price"], 2500)

    def test_refresh_holding_prices_keeps_input_when_lookup_fails(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.save_forward_snapshot._fetch_latest_close",
            side_effect=RuntimeError("lookup failed"),
        ):
            holdings = _refresh_holding_prices(
                [{"symbol": "005930", "qty": 1, "avg_cost": 2000, "current_price": 2000}]
            )

        self.assertEqual(holdings[0]["current_price"], 2000)


if __name__ == "__main__":
    unittest.main()
