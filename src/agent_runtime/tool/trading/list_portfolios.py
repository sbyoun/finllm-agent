"""list_portfolios tool: read user's paper-trading portfolios with positions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition

from .place_trade import _supabase_request


@dataclass(slots=True)
class ListPortfoliosAction(Action):
    include_positions: bool = True

    def to_arguments_json(self) -> str:
        return json.dumps({"include_positions": self.include_positions}, ensure_ascii=False)


@dataclass(slots=True)
class ListPortfoliosObservation(Observation):
    success: bool = False
    message: str = ""
    portfolios: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        if not self.success:
            return f"포트폴리오 조회 실패: {self.message}"
        if not self.portfolios:
            return "보유한 포트폴리오가 없습니다."
        lines = []
        for p in self.portfolios:
            primary = " *primary*" if p.get("is_primary") else ""
            lines.append(f"- [{p['id']}] {p.get('name','')}{primary}")
            for pos in p.get("positions", []):
                name = pos.get("name") or ""
                label = f"{name}({pos['symbol']})" if name else pos["symbol"]
                lines.append(
                    f"    · {label} {pos['qty']}주 평균 {float(pos['avg_cost']):,.2f}원"
                )
        return "\n".join(lines)


def _execute(action: ListPortfoliosAction, conversation: Any) -> ListPortfoliosObservation:
    state = conversation.state if conversation is not None else None
    user_id = state.get_agent_state("user_id") if state else None
    if not user_id:
        return ListPortfoliosObservation(
            success=False, message="페이퍼 트레이딩은 회원 전용 기능입니다."
        )
    try:
        rows = _supabase_request(
            f"trading_portfolios?user_id=eq.{user_id}&select=id,name,is_primary,created_at,updated_at&order=is_primary.desc,created_at.asc"
        ) or []
    except Exception as exc:  # noqa: BLE001
        return ListPortfoliosObservation(success=False, message=str(exc))

    if action.include_positions and rows:
        ids = ",".join(r["id"] for r in rows)
        try:
            positions = _supabase_request(
                f"trading_positions?portfolio_id=in.({ids})&select=portfolio_id,symbol,name,qty,avg_cost"
            ) or []
        except Exception:
            positions = []
        by_pid: dict[str, list[dict]] = {}
        for pos in positions:
            by_pid.setdefault(pos["portfolio_id"], []).append(pos)
        for r in rows:
            r["positions"] = by_pid.get(r["id"], [])

    return ListPortfoliosObservation(success=True, portfolios=rows)


@dataclass(slots=True)
class ListPortfoliosTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "include_positions": {
                    "type": "boolean",
                    "description": "각 포트폴리오의 보유 종목까지 함께 반환할지 여부 (기본 true).",
                }
            },
        }


def make_list_portfolios_tool() -> ListPortfoliosTool:
    return ListPortfoliosTool(
        name="list_portfolios",
        description=(
            "사용자의 페이퍼 트레이딩 포트폴리오 목록과 (옵션) 보유 종목을 조회한다. "
            "기존 특정 포트폴리오에 매매하거나 비교/요약할 때 먼저 호출해 portfolio_id 와 "
            "현황을 파악하라."
        ),
        action_type=ListPortfoliosAction,
        observation_type=ListPortfoliosObservation,
        executor=_execute,
    )
