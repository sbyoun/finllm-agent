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
        body: dict[str, Any] = {
            "forward_test_id": action.forward_test_id,
            "holdings": action.holdings,
            "cash": action.cash,
            "total_value": action.total_value,
            "return_pct": action.return_pct,
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
            f"평가액 {action.total_value:,.0f} | 수익률 {action.return_pct:+.2f}%"
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
                    "description": "현재 보유 종목 목록. 각 항목: {symbol, name, qty, avg_cost, current_price, weight_pct}",
                    "items": {"type": "object"},
                },
                "cash": {
                    "type": "number",
                    "description": "보유 현금",
                },
                "total_value": {
                    "type": "number",
                    "description": "총 평가액 (holdings 시가 + cash)",
                },
                "return_pct": {
                    "type": "number",
                    "description": "초기 자본 대비 누적 수익률 (%)",
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
            "required": ["forward_test_id", "holdings", "cash", "total_value", "return_pct"],
        }


def make_save_forward_snapshot_tool() -> SaveForwardSnapshotTool:
    return SaveForwardSnapshotTool(
        name="save_forward_snapshot",
        description=(
            "포워드 테스트의 리밸런싱 결과를 스냅샷으로 저장합니다. "
            "매 리밸런싱 후 반드시 호출해야 합니다. "
            "보유 종목, 현금, 총 평가액, 수익률, 매매 내역을 기록합니다."
        ),
        action_type=SaveForwardSnapshotAction,
        observation_type=SaveForwardSnapshotObservation,
        executor=_execute,
    )
