import unittest
from unittest.mock import patch

from agent_runtime.tool.forward_test.execute_forward_trades import (
    _build_trades_from_orders,
    _execute_orders,
)


class ExecuteForwardTradesTests(unittest.TestCase):
    def test_budget_pct_buy_builds_trade_with_quoted_price(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.execute_forward_trades._fetch_domestic_current_price",
            return_value=1000,
        ):
            trades, errors = _build_trades_from_orders(
                [{"symbol": "005930", "name": "삼성전자", "side": "buy", "budget_pct": 50}],
                starting_cash=10_000,
                previous_holdings=[],
            )

        self.assertEqual(errors, [])
        self.assertEqual(trades[0]["qty"], 5)
        self.assertEqual(trades[0]["price"], 1000)
        self.assertEqual(trades[0]["amount"], 5000)

    def test_sell_without_qty_sells_entire_position(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.execute_forward_trades._fetch_domestic_current_price",
            return_value=1500,
        ):
            trades, errors = _build_trades_from_orders(
                [{"symbol": "005930", "side": "sell"}],
                starting_cash=1000,
                previous_holdings=[{"symbol": "005930", "name": "삼성전자", "qty": 3, "avg_cost": 1000}],
            )

        self.assertEqual(errors, [])
        self.assertEqual(trades[0]["qty"], 3)
        self.assertEqual(trades[0]["amount"], 4500)

    def test_buy_rejects_cash_overrun(self) -> None:
        with patch(
            "agent_runtime.tool.forward_test.execute_forward_trades._fetch_domestic_current_price",
            return_value=1000,
        ):
            trades, errors = _build_trades_from_orders(
                [{"symbol": "005930", "side": "buy", "qty": 11}],
                starting_cash=10_000,
                previous_holdings=[],
            )

        self.assertEqual(trades, [])
        self.assertTrue(errors)

    def test_execute_orders_saves_computed_snapshot(self) -> None:
        saved_body = {}

        def fake_supabase(path: str, *, method: str = "GET", body: dict | None = None):
            if method == "POST" and path == "forward_snapshots":
                saved_body.update(body or {})
                return [{"id": "snap-1"}]
            raise AssertionError(f"unexpected request: {method} {path}")

        with (
            patch("agent_runtime.tool.forward_test.execute_forward_trades._fetch_initial_capital", return_value=10_000),
            patch("agent_runtime.tool.forward_test.execute_forward_trades._fetch_latest_snapshot", return_value=None),
            patch("agent_runtime.tool.forward_test.execute_forward_trades._fetch_domestic_current_price", return_value=1000),
            patch("agent_runtime.tool.forward_test.execute_forward_trades._supabase_request", side_effect=fake_supabase),
        ):
            success, message, snapshot_id = _execute_orders(
                forward_test_id="ft-1",
                orders=[{"symbol": "005930", "name": "삼성전자", "side": "buy", "budget_amount": 5000}],
                reasoning="test",
            )

        self.assertTrue(success, message)
        self.assertEqual(snapshot_id, "snap-1")
        self.assertEqual(saved_body["cash"], 5000)
        self.assertEqual(saved_body["total_value"], 10_000)
        self.assertEqual(saved_body["return_pct"], 0)
        self.assertEqual(saved_body["holdings"][0]["qty"], 5)
        self.assertEqual(saved_body["holdings"][0]["avg_cost"], 1000)
        self.assertEqual(saved_body["trades"][0]["amount"], 5000)


if __name__ == "__main__":
    unittest.main()
