"""execute_forward_trades tool: apply paper-trade orders to a forward-test ledger."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError

from agent_runtime.tool.forward_test.save_forward_snapshot import (
    _as_float,
    _build_holdings_from_ledger,
    _compute_cash_after_trades,
    _compute_return_pct,
    _compute_total_value,
    _fetch_domestic_current_price,
    _fetch_initial_capital,
    _fetch_latest_snapshot,
    _first_number,
    _supabase_request,
    _trade_amount,
    _trade_side,
)
from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


MAX_ORDERS = 10


def _normalize_quantity(value: Any) -> float:
    if isinstance(value, str) and value.strip().lower() in {"all", "전량", "전체"}:
        return -1
    return _as_float(value, default=0)


def _order_quantity(order: dict) -> float:
    for key in ("qty", "shares", "quantity", "units"):
        if key in order:
            return _normalize_quantity(order.get(key))
    return 0


def _order_budget_amount(order: dict, starting_cash: float) -> float:
    budget = _as_float(order.get("budget_amount"), default=-1)
    if budget >= 0:
        return budget
    budget = _as_float(order.get("amount"), default=-1)
    if budget >= 0:
        return budget
    budget_pct = _as_float(order.get("budget_pct"), default=-1)
    if budget_pct >= 0:
        return starting_cash * budget_pct / 100
    return -1


def _quote_price(symbol: str, order: dict) -> float:
    supplied = _first_number(order, ("price", "current_price", "execution_price"))
    try:
        quoted = _fetch_domestic_current_price(symbol)
    except Exception:
        quoted = 0
    return quoted if quoted > 0 else supplied


def _position_qty_by_symbol(holdings: list[dict]) -> dict[str, float]:
    result: dict[str, float] = {}
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").strip()
        qty = _first_number(holding, ("qty", "shares", "quantity", "units"))
        if symbol and qty > 0:
            result[symbol] = result.get(symbol, 0) + qty
    return result


def _name_by_symbol(holdings: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").strip()
        name = str(holding.get("name") or "").strip()
        if symbol and name:
            result[symbol] = name
    return result


def _build_trades_from_orders(
    orders: list[dict],
    *,
    starting_cash: float,
    previous_holdings: list[dict],
) -> tuple[list[dict], list[str]]:
    if len(orders) > MAX_ORDERS:
        return [], [f"한 번에 최대 {MAX_ORDERS}개 주문까지만 실행할 수 있습니다."]

    cash = starting_cash
    qty_by_symbol = _position_qty_by_symbol(previous_holdings)
    names = _name_by_symbol(previous_holdings)
    trades: list[dict] = []
    errors: list[str] = []

    for index, order in enumerate(orders, start=1):
        if not isinstance(order, dict):
            errors.append(f"orders[{index}] 형식이 올바르지 않습니다.")
            continue

        side = _trade_side(order)
        symbol = str(order.get("symbol") or "").strip()
        if side not in {"buy", "sell"}:
            errors.append(f"orders[{index}] 매매 구분은 buy 또는 sell이어야 합니다.")
            continue
        if not symbol:
            errors.append(f"orders[{index}] symbol이 필요합니다.")
            continue

        price = _quote_price(symbol, order)
        if price <= 0:
            errors.append(f"{symbol} 현재가를 확인할 수 없어 주문을 실행하지 않았습니다.")
            continue

        qty = _order_quantity(order)
        if side == "buy":
            if qty <= 0:
                budget = _order_budget_amount(order, starting_cash)
                if budget <= 0:
                    errors.append(f"{symbol} 매수에는 qty, budget_amount, budget_pct 중 하나가 필요합니다.")
                    continue
                qty = math.floor(budget / price)
            if qty <= 0:
                errors.append(f"{symbol} 매수 수량이 0입니다.")
                continue
            amount = qty * price
            if amount > cash + 1e-6:
                errors.append(
                    f"{symbol} 매수 금액이 가용 현금을 초과합니다. "
                    f"필요 {amount:,.0f}, 가용 {cash:,.0f}"
                )
                continue
            cash -= amount
            qty_by_symbol[symbol] = qty_by_symbol.get(symbol, 0) + qty
        else:
            held_qty = qty_by_symbol.get(symbol, 0)
            if qty < 0 or qty == 0:
                qty = held_qty
            if qty <= 0:
                errors.append(f"{symbol} 매도할 보유 수량이 없습니다.")
                continue
            if qty > held_qty + 1e-6:
                errors.append(
                    f"{symbol} 매도 수량이 보유 수량을 초과합니다. "
                    f"보유 {held_qty:g}, 매도 {qty:g}"
                )
                continue
            amount = qty * price
            cash += amount
            remaining = held_qty - qty
            if remaining <= 1e-6:
                qty_by_symbol.pop(symbol, None)
            else:
                qty_by_symbol[symbol] = remaining

        trade = {
            "symbol": symbol,
            "name": order.get("name") or names.get(symbol) or symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "amount": amount,
        }
        if order.get("reason"):
            trade["reason"] = order.get("reason")
        trades.append(trade)

    return trades, errors


def _mark_holdings_to_market(holdings: list[dict]) -> list[dict]:
    marked: list[dict] = []
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        item = dict(holding)
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            try:
                price = _fetch_domestic_current_price(symbol)
            except Exception:
                price = 0
            if price > 0:
                item["current_price"] = price
        marked.append(item)
    return marked


def _execute_orders(
    *,
    forward_test_id: str,
    orders: list[dict],
    reasoning: str | None,
) -> tuple[bool, str, str | None]:
    initial_capital = _fetch_initial_capital(forward_test_id)
    if initial_capital <= 0:
        return False, "forward_test initial_capital을 확인할 수 없습니다.", None

    latest_snapshot = _fetch_latest_snapshot(forward_test_id)
    previous_holdings: list[dict] = []
    if isinstance(latest_snapshot, dict) and isinstance(latest_snapshot.get("holdings"), list):
        previous_holdings = latest_snapshot["holdings"]
    starting_cash = _as_float(latest_snapshot.get("cash")) if latest_snapshot else initial_capital

    trades, errors = _build_trades_from_orders(
        orders,
        starting_cash=starting_cash,
        previous_holdings=previous_holdings,
    )
    if errors:
        return False, "; ".join(errors[:5]), None

    if not trades and not previous_holdings:
        return False, "첫 스냅샷에는 최소 1개 주문이 필요합니다.", None

    cash = _compute_cash_after_trades(starting_cash, trades, fallback_cash=starting_cash)
    if cash < -1:
        return False, f"매수 금액이 가용 현금을 초과합니다. 계산 현금: {cash:,.0f}", None

    holdings, ledger_errors = _build_holdings_from_ledger(previous_holdings, trades, None)
    if ledger_errors:
        return False, "; ".join(ledger_errors[:5]), None
    holdings = _mark_holdings_to_market(holdings)

    total_value = _compute_total_value(holdings, cash)
    return_pct = _compute_return_pct(total_value, initial_capital)
    body: dict[str, Any] = {
        "forward_test_id": forward_test_id,
        "holdings": holdings,
        "cash": cash,
        "total_value": total_value,
        "return_pct": return_pct,
    }
    if trades:
        body["trades"] = trades
    if reasoning:
        body["reasoning"] = reasoning

    result = _supabase_request("forward_snapshots", method="POST", body=body)
    snap_id = result[0]["id"] if isinstance(result, list) and result else None
    traded_amount = sum(_trade_amount(t) for t in trades)
    return (
        True,
        (
            f"매매 {len(trades)}건 | 보유 {len(holdings)}종목 | "
            f"거래금액 {traded_amount:,.0f} | 현금 {cash:,.0f} | "
            f"평가액 {total_value:,.0f} | 수익률 {return_pct:+.2f}%"
        ),
        snap_id,
    )


@dataclass(slots=True)
class ExecuteForwardTradesAction(Action):
    forward_test_id: str = ""
    orders: list[dict] | None = None
    reasoning: str | None = None

    def to_arguments_json(self) -> str:
        return json.dumps(
            {
                "forward_test_id": self.forward_test_id,
                "orders": self.orders or [],
                "reasoning": self.reasoning,
            },
            ensure_ascii=False,
        )


@dataclass(slots=True)
class ExecuteForwardTradesObservation(Observation):
    success: bool = False
    message: str = ""
    snapshot_id: str | None = None

    def to_text(self) -> str:
        if self.success:
            return f"포워드 테스트 주문이 기록되었습니다 (snapshot_id={self.snapshot_id}). {self.message}"
        return f"포워드 테스트 주문 실패: {self.message}"


def _execute(action: ExecuteForwardTradesAction, conversation: Any) -> ExecuteForwardTradesObservation:
    if not action.forward_test_id:
        return ExecuteForwardTradesObservation(success=False, message="forward_test_id가 필요합니다.")
    orders = action.orders or []
    if not isinstance(orders, list):
        return ExecuteForwardTradesObservation(success=False, message="orders는 배열이어야 합니다.")

    try:
        success, message, snapshot_id = _execute_orders(
            forward_test_id=action.forward_test_id,
            orders=orders,
            reasoning=action.reasoning,
        )
        return ExecuteForwardTradesObservation(success=success, message=message, snapshot_id=snapshot_id)
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return ExecuteForwardTradesObservation(success=False, message=f"저장 실패: {body_text[:200]}")
    except Exception as exc:
        return ExecuteForwardTradesObservation(success=False, message=f"저장 실패: {exc}")


@dataclass(slots=True)
class ExecuteForwardTradesTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "forward_test_id": {
                    "type": "string",
                    "description": "포워드 테스트 ID",
                },
                "orders": {
                    "type": "array",
                    "description": (
                        "이번 실행에서 수행할 주문 의도 목록. 최대 10건. "
                        "매수는 qty 또는 budget_amount 또는 budget_pct 중 하나를 지정합니다. "
                        "매도는 qty를 생략하면 해당 종목 전량 매도합니다. "
                        "가격, 현금, 보유, 평단, 수익률은 도구가 계산합니다."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string", "description": "종목 코드"},
                            "name": {"type": "string", "description": "종목명"},
                            "side": {"type": "string", "enum": ["buy", "sell"], "description": "buy 또는 sell"},
                            "qty": {"type": "number", "description": "주문 수량. sell에서 생략하면 전량 매도"},
                            "budget_amount": {"type": "number", "description": "매수 예산 금액"},
                            "budget_pct": {"type": "number", "description": "실행 시작 시점 가용 현금 대비 매수 예산 비율"},
                            "reason": {"type": "string", "description": "주문 판단 근거"},
                        },
                        "required": ["symbol", "side"],
                    },
                },
                "reasoning": {
                    "type": "string",
                    "description": "이번 실행의 종합 판단 근거",
                },
            },
            "required": ["forward_test_id", "orders"],
        }


def make_execute_forward_trades_tool() -> ExecuteForwardTradesTool:
    return ExecuteForwardTradesTool(
        name="execute_forward_trades",
        description=(
            "포워드 테스트 주문 의도를 실행하고 원장 스냅샷을 저장합니다. "
            "LLM은 주문 의도만 전달하고, 도구가 현재가 조회, 수량/현금 검증, "
            "보유/평단/총 평가액/수익률 계산, 스냅샷 저장을 수행합니다. "
            "가용 현금 초과 매수, 보유 초과 매도, 10건 초과 주문은 거부합니다."
        ),
        action_type=ExecuteForwardTradesAction,
        observation_type=ExecuteForwardTradesObservation,
        executor=_execute,
    )
