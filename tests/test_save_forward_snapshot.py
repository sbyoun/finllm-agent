import unittest
from unittest.mock import patch

from agent_runtime.tool.forward_test.save_forward_snapshot import (
    _apply_trade_prices_to_holdings,
    _complete_initial_buy_trades,
    _compute_first_snapshot_cash,
    _compute_cash_after_trades,
    _compute_return_pct,
    _compute_total_value,
    _normalize_trades,
    _refresh_trade_prices,
    _starting_cash,
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

    def test_starting_cash_preserves_negative_previous_cash(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.save_forward_snapshot._fetch_latest_snapshot_cash",
            return_value=-4202430,
        ):
            self.assertEqual(_starting_cash("ft-1", 100_000_000), -4202430)

    def test_first_snapshot_cash_uses_all_final_holdings(self) -> None:
        cash = _compute_first_snapshot_cash(
            100_000_000,
            [
                {"symbol": "AAA", "qty": 10, "current_price": 5_000_000},
                {"symbol": "BBB", "qty": 5, "current_price": 8_000_000},
            ],
        )

        self.assertEqual(cash, 10_000_000)

    def test_normalize_trades_repairs_side_and_missing_symbol_from_holdings(self) -> None:
        trades = _normalize_trades(
            [{"name": "삼성중공업", "side": "buy,symbol:", "qty": 145, "price": 34400}],
            [{"name": "삼성중공업", "symbol": "010140", "qty": 145, "current_price": 34400}],
        )

        self.assertEqual(trades[0]["side"], "buy")
        self.assertEqual(trades[0]["symbol"], "010140")

    def test_complete_initial_buy_trades_adds_missing_holdings(self) -> None:
        trades = _complete_initial_buy_trades(
            [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 1000}],
            [
                {"symbol": "AAA", "name": "A", "qty": 1, "current_price": 1000},
                {"symbol": "BBB", "name": "B", "qty": 2, "current_price": 1500},
            ],
        )

        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[1]["symbol"], "BBB")
        self.assertEqual(trades[1]["side"], "buy")
        self.assertEqual(trades[1]["qty"], 2)
        self.assertEqual(trades[1]["price"], 1500)

    def test_refresh_trade_prices_uses_kis_quote_for_kr_ticker(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.save_forward_snapshot._fetch_domestic_current_price",
            return_value=72000,
        ):
            trades = _refresh_trade_prices(
                [{"symbol": "005930", "side": "sell", "qty": 2, "price": 70000}]
            )

        self.assertEqual(trades[0]["price"], 72000)

    def test_refresh_trade_prices_falls_back_to_input_price_on_quote_failure(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.save_forward_snapshot._fetch_domestic_current_price",
            side_effect=RuntimeError("quote failed"),
        ):
            trades = _refresh_trade_prices(
                [{"symbol": "005930", "side": "sell", "qty": 2, "price": 70000}]
            )

        self.assertEqual(trades[0]["price"], 70000)


if __name__ == "__main__":
    unittest.main()
