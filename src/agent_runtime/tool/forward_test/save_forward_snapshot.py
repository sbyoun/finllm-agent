"""save_forward_snapshot tool: save a rebalancing snapshot for a forward test."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_KIS_CLIENT: Any | None = None


def _as_float(value: Any, default: float = 0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_number(item: dict, keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _as_float(item.get(key), default=-1)
        if value >= 0:
            return value
    return 0


def _holding_market_value(holding: dict) -> float:
    qty = _first_number(holding, ("qty", "shares", "quantity", "units"))
    price = _first_number(
        holding,
        (
            "current_price",
            "mark_price",
            "close_price",
            "price",
            "avg_cost",
            "avg_price",
        ),
    )
    return qty * price


def _compute_total_value(holdings: list[dict], cash: Any) -> float:
    holdings_value = sum(_holding_market_value(h) for h in holdings if isinstance(h, dict))
    return _as_float(cash) + holdings_value


def _compute_return_pct(total_value: float, initial_capital: Any, fallback: Any = 0) -> float:
    initial = _as_float(initial_capital)
    if initial <= 0:
        return _as_float(fallback)
    return round(((total_value / initial) - 1) * 100, 6)


def _trade_side(trade: dict) -> str:
    raw = str(trade.get("side") or trade.get("action") or "").strip().lower()
    if raw in {"매수", "buy", "b"} or "매수" in raw or raw.startswith("buy"):
        return "buy"
    if raw in {"매도", "sell", "s"} or "매도" in raw or raw.startswith("sell"):
        return "sell"
    return raw


def _normalize_trades(trades: list[dict] | None, holdings: list[dict] | None) -> list[dict] | None:
    if not trades:
        return trades

    symbol_by_name: dict[str, str] = {}
    name_by_symbol: dict[str, str] = {}
    for holding in holdings or []:
        if not isinstance(holding, dict):
            continue
        name = str(holding.get("name") or "").strip()
        symbol = str(holding.get("symbol") or "").strip()
        if name and symbol:
            symbol_by_name[name] = symbol
            name_by_symbol[symbol] = name

    normalized: list[dict] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        item = dict(trade)
        side = _trade_side(item)
        if side in {"buy", "sell"}:
            item["side"] = side

        symbol = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        if not symbol and name in symbol_by_name:
            item["symbol"] = symbol_by_name[name]
        if not name and symbol in name_by_symbol:
            item["name"] = name_by_symbol[symbol]
        normalized.append(item)

    return normalized


def _complete_initial_buy_trades(trades: list[dict] | None, holdings: list[dict]) -> list[dict] | None:
    if not holdings:
        return trades

    completed = list(trades or [])
    traded_symbols = {
        str(trade.get("symbol") or "").strip()
        for trade in completed
        if isinstance(trade, dict) and _trade_side(trade) == "buy"
    }

    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").strip()
        if not symbol or symbol in traded_symbols:
            continue
        qty = _first_number(holding, ("qty", "shares", "quantity", "units"))
        price = _first_number(
            holding,
            (
                "current_price",
                "mark_price",
                "close_price",
                "price",
                "avg_cost",
                "avg_price",
            ),
        )
        if qty <= 0 or price <= 0:
            continue
        completed.append(
            {
                "symbol": symbol,
                "name": holding.get("name") or symbol,
                "side": "buy",
                "qty": qty,
                "price": price,
                "reason": "초기 포워드 테스트 편입",
            }
        )
        traded_symbols.add(symbol)

    return completed


def _trade_amount(trade: dict) -> float:
    amount = _as_float(trade.get("amount"), default=-1)
    if amount >= 0:
        return amount
    qty = _first_number(trade, ("qty", "shares", "quantity", "units"))
    price = _first_number(trade, ("price", "current_price", "execution_price"))
    return qty * price


def _compute_cash_after_trades(starting_cash: Any, trades: list[dict], fallback_cash: Any) -> float:
    cash = _as_float(starting_cash, default=-1)
    if cash < 0:
        return _as_float(fallback_cash)

    for trade in trades:
        if not isinstance(trade, dict):
            continue
        amount = _trade_amount(trade)
        side = _trade_side(trade)
        if side == "buy":
            cash -= amount
        elif side == "sell":
            cash += amount
    return cash


def _apply_trade_prices_to_holdings(holdings: list[dict], trades: list[dict] | None) -> list[dict]:
    latest_trade_price: dict[str, float] = {}
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        symbol = str(trade.get("symbol", "")).strip()
        price = _first_number(trade, ("price", "current_price", "execution_price"))
        if symbol and price > 0:
            latest_trade_price[symbol] = price

    updated: list[dict] = []
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        item = dict(holding)
        symbol = str(item.get("symbol", "")).strip()
        if symbol in latest_trade_price:
            item["current_price"] = latest_trade_price[symbol]
        updated.append(item)
    return updated


def _get_kis_client() -> Any:
    global _KIS_CLIENT
    if _KIS_CLIENT is None:
        from alpha_engine.kis.simpleki import SimpleKI
        from alpha_engine.settings import settings

        _KIS_CLIENT = SimpleKI(settings.KIS_KEYFILE_PATH)
    return _KIS_CLIENT


def _is_kr_ticker(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) <= 6


def _fetch_domestic_current_price(symbol: str) -> float:
    if not _is_kr_ticker(symbol):
        return 0
    output = _get_kis_client().get_current_price_domestic(symbol.zfill(6))
    return _as_float((output or {}).get("stck_prpr"))


def _refresh_trade_prices(trades: list[dict] | None) -> list[dict] | None:
    if not trades:
        return trades

    latest_by_symbol: dict[str, float] = {}
    refreshed: list[dict] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        item = dict(trade)
        symbol = str(item.get("symbol", "")).strip()
        if symbol:
            if symbol not in latest_by_symbol:
                try:
                    latest_by_symbol[symbol] = _fetch_domestic_current_price(symbol)
                except Exception:
                    latest_by_symbol[symbol] = 0
            if latest_by_symbol[symbol] > 0:
                item["price"] = latest_by_symbol[symbol]
        refreshed.append(item)
    return refreshed


def _supabase_request(path: str, *, method: str = "GET", body: dict | None = None) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _fetch_initial_capital(forward_test_id: str) -> float:
    result = _supabase_request(
        f"forward_tests?id=eq.{forward_test_id}&select=initial_capital&limit=1"
    )
    if isinstance(result, list) and result:
        return _as_float(result[0].get("initial_capital"))
    return 0


def _fetch_latest_snapshot_cash(forward_test_id: str) -> float | None:
    result = _supabase_request(
        f"forward_snapshots?forward_test_id=eq.{forward_test_id}"
        "&select=cash&order=snapshot_at.desc&limit=1"
    )
    if isinstance(result, list) and result:
        return _as_float(result[0].get("cash"))
    return None


def _starting_cash(forward_test_id: str, initial_capital: float) -> float:
    previous_cash = _fetch_latest_snapshot_cash(forward_test_id)
    if previous_cash is not None:
        return previous_cash
    return initial_capital


def _is_first_snapshot(forward_test_id: str) -> bool:
    return _fetch_latest_snapshot_cash(forward_test_id) is None


def _compute_first_snapshot_cash(initial_capital: float, holdings: list[dict]) -> float:
    return initial_capital - _compute_total_value(holdings, 0)


@dataclass(slots=True)
class SaveForwardSnapshotAction(Action):
    forward_test_id: str = ""
    holdings: list[dict] = None  # type: ignore[assignment]
    cash: float = 0
    total_value: float = 0
    return_pct: float = 0
    trades: list[dict] | None = None
    reasoning: str | None = None

    def __post_init__(self) -> None:
        if self.holdings is None:
            self.holdings = []

    def to_arguments_json(self) -> str:
        d: dict[str, Any] = {
            "forward_test_id": self.forward_test_id,
            "holdings": self.holdings,
            "cash": self.cash,
            "total_value": self.total_value,
            "return_pct": self.return_pct,
        }
        if self.trades:
            d["trades"] = self.trades
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return json.dumps(d, ensure_ascii=False)


@dataclass(slots=True)
class SaveForwardSnapshotObservation(Observation):
    success: bool = False
    message: str = ""
    snapshot_id: str | None = None

    def to_text(self) -> str:
        if self.success:
            return f"스냅샷이 저장되었습니다 (id={self.snapshot_id}). {self.message}"
        return f"스냅샷 저장 실패: {self.message}"


def _execute(action: SaveForwardSnapshotAction, conversation: Any) -> SaveForwardSnapshotObservation:
    if not action.forward_test_id:
        return SaveForwardSnapshotObservation(
            success=False, message="forward_test_id가 필요합니다."
        )
    if not action.holdings and action.cash == 0:
        return SaveForwardSnapshotObservation(
            success=False, message="holdings 또는 cash가 필요합니다."
        )

    try:
        initial_capital = _fetch_initial_capital(action.forward_test_id)
        is_first_snapshot = _is_first_snapshot(action.forward_test_id)
        trades = _normalize_trades(action.trades, action.holdings)
        if is_first_snapshot:
            trades = _complete_initial_buy_trades(trades, action.holdings)
        trades = _refresh_trade_prices(trades)
        holdings = _apply_trade_prices_to_holdings(action.holdings, trades)
        cash = action.cash
        if is_first_snapshot and holdings:
            # On the initial snapshot there are no prior positions. The final
            # holdings define the invested amount, even if the model omitted
            # some trade rows or supplied stale cash.
            cash = _compute_first_snapshot_cash(initial_capital, holdings)
        elif trades:
            cash = _compute_cash_after_trades(
                _starting_cash(action.forward_test_id, initial_capital),
                trades,
                fallback_cash=action.cash,
            )
        total_value = _compute_total_value(holdings, cash)
        if total_value <= 0 and action.total_value:
            total_value = _as_float(action.total_value)
        return_pct = _compute_return_pct(
            total_value,
            initial_capital,
            fallback=action.return_pct,
        )

        body: dict[str, Any] = {
            "forward_test_id": action.forward_test_id,
            "holdings": holdings,
            "cash": cash,
            "total_value": total_value,
            "return_pct": return_pct,
        }
        if trades:
            body["trades"] = trades
        if action.reasoning:
            body["reasoning"] = action.reasoning

        result = _supabase_request("forward_snapshots", method="POST", body=body)
        snap_id = result[0]["id"] if isinstance(result, list) and result else None

        # Build summary
        n_holdings = len(action.holdings)
        n_trades = len(trades) if trades else 0
        summary = (
            f"보유 {n_holdings}종목 | 매매 {n_trades}건 | "
            f"평가액 {total_value:,.0f} | 수익률 {return_pct:+.2f}%"
        )

        return SaveForwardSnapshotObservation(
            success=True, message=summary, snapshot_id=snap_id
        )

    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return SaveForwardSnapshotObservation(
            success=False, message=f"저장 실패: {body_text[:200]}"
        )
    except Exception as exc:
        return SaveForwardSnapshotObservation(
            success=False, message=f"저장 실패: {exc}"
        )


@dataclass(slots=True)
class SaveForwardSnapshotTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "forward_test_id": {
                    "type": "string",
                    "description": "포워드 테스트 ID",
                },
                "holdings": {
                    "type": "array",
                    "description": "현재 보유 종목 목록. 각 항목: {symbol, name, qty, avg_cost, current_price, weight_pct}. 리밸런싱에서 매매한 종목은 체결가를 current_price로 사용합니다.",
                    "items": {"type": "object"},
                },
                "cash": {
                    "type": "number",
                    "description": "보유 현금",
                },
                "total_value": {
                    "type": "number",
                    "description": "총 평가액 (서버에서 holdings 시가 + cash로 재계산됨)",
                },
                "return_pct": {
                    "type": "number",
                    "description": "초기 자본 대비 누적 수익률 (%) (서버에서 재계산됨)",
                },
                "trades": {
                    "type": "array",
                    "description": "이번 리밸런싱 매매 내역. 각 항목: {symbol, name, side, qty, price, reason}. 국내 6자리 티커는 서버가 KIS 현재가로 price를 보정합니다.",
                    "items": {"type": "object"},
                },
                "reasoning": {
                    "type": "string",
                    "description": "LLM 기반 전략일 때 판단 근거",
                },
            },
            "required": ["forward_test_id", "holdings", "cash"],
        }


def make_save_forward_snapshot_tool() -> SaveForwardSnapshotTool:
    return SaveForwardSnapshotTool(
        name="save_forward_snapshot",
        description=(
            "포워드 테스트의 리밸런싱 결과를 스냅샷으로 저장합니다. "
            "매 리밸런싱 후 반드시 호출해야 합니다. "
            "보유 종목, 현금, 총 평가액, 수익률, 매매 내역을 기록합니다. "
            "총 평가액과 수익률은 서버가 보유 종목 평가가와 현금으로 재계산합니다."
        ),
        action_type=SaveForwardSnapshotAction,
        observation_type=SaveForwardSnapshotObservation,
        executor=_execute,
    )
