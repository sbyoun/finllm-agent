import unittest

from agent_runtime.tool.forward_test.save_forward_snapshot import (
    _apply_trade_prices_to_holdings,
    _compute_cash_after_trades,
    _compute_return_pct,
    _compute_total_value,
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

    def test_apply_trade_prices_marks_touched_holdings_at_execution_price(self) -> None:
        holdings = _apply_trade_prices_to_holdings(
            [{"symbol": "005930", "qty": 1, "avg_cost": 2000, "current_price": 2000}],
            [{"symbol": "005930", "side": "buy", "qty": 1, "price": 2500}],
        )

        self.assertEqual(holdings[0]["current_price"], 2500)

    def test_compute_cash_after_trades_uses_trade_execution_prices(self) -> None:
        cash = _compute_cash_after_trades(
            10000,
            [
                {"symbol": "AAA", "side": "매도", "qty": 2, "price": 1500},
                {"symbol": "BBB", "side": "매수", "qty": 1, "price": 4000},
            ],
            fallback_cash=0,
        )

        self.assertEqual(cash, 9000)


if __name__ == "__main__":
    unittest.main()
