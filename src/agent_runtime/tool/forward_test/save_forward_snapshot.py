"""save_forward_snapshot tool: save a rebalancing snapshot for a forward test."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


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


def _fetch_latest_close(symbol: str) -> float:
    ticker = quote(symbol.strip(), safe="")
    stocks = _supabase_request(f"stocks?ticker=eq.{ticker}&select=id,ticker&limit=1")
    if not isinstance(stocks, list) or not stocks:
        return 0

    stock_id = quote(str(stocks[0].get("id", "")).strip(), safe="")
    if not stock_id:
        return 0

    prices = _supabase_request(
        f"daily_prices?stock_id=eq.{stock_id}&select=close,date&order=date.desc&limit=1"
    )
    if isinstance(prices, list) and prices:
        return _as_float(prices[0].get("close"))
    return 0


def _refresh_holding_prices(holdings: list[dict]) -> list[dict]:
    refreshed: list[dict] = []
    latest_by_symbol: dict[str, float] = {}

    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        item = dict(holding)
        symbol = str(item.get("symbol", "")).strip()
        if symbol:
            if symbol not in latest_by_symbol:
                try:
                    latest_by_symbol[symbol] = _fetch_latest_close(symbol)
                except Exception:
                    latest_by_symbol[symbol] = 0
            if latest_by_symbol[symbol] > 0:
                item["current_price"] = latest_by_symbol[symbol]
        refreshed.append(item)

    return refreshed


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
        holdings = _refresh_holding_prices(action.holdings)
        total_value = _compute_total_value(holdings, action.cash)
        if total_value <= 0 and action.total_value:
            total_value = _as_float(action.total_value)
        return_pct = _compute_return_pct(
            total_value,
            _fetch_initial_capital(action.forward_test_id),
            fallback=action.return_pct,
        )

        body: dict[str, Any] = {
            "forward_test_id": action.forward_test_id,
            "holdings": holdings,
            "cash": action.cash,
            "total_value": total_value,
            "return_pct": return_pct,
        }
        if action.trades:
            body["trades"] = action.trades
        if action.reasoning:
            body["reasoning"] = action.reasoning

        result = _supabase_request("forward_snapshots", method="POST", body=body)
        snap_id = result[0]["id"] if isinstance(result, list) and result else None

        # Build summary
        n_holdings = len(action.holdings)
        n_trades = len(action.trades) if action.trades else 0
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
                    "description": "현재 보유 종목 목록. 각 항목: {symbol, name, qty, avg_cost, current_price, weight_pct}. current_price는 실제 최신 평가가이며 avg_cost를 임의로 복사하지 않습니다.",
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
                    "description": "이번 리밸런싱 매매 내역. 각 항목: {symbol, name, side, qty, price, reason}",
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
