from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from agent_runtime.env import require_env
from agent_runtime.kis.quote import kis_quote
from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


@dataclass(slots=True)
class GetPortfolioAction(Action):
    portfolio_id: str = ""
    include_all: bool = False


@dataclass(slots=True)
class GetPortfolioObservation(Observation):
    user_id: str = ""
    session_id: str = ""
    portfolios: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    def to_text(self) -> str:
        if self.message and not self.rows:
            return self.message
        lines = [f"user_id={self.user_id}"]
        for pf in self.portfolios:
            lines.append(
                f"[포트폴리오] {pf.get('name')} (id={pf.get('id')}, primary={pf.get('is_primary')})"
            )
        if not self.rows:
            lines.append("(보유 종목 없음)")
            return "\n".join(lines)
        lines.append(
            "symbol | qty | avg_cost | current_price | market_value | unrealized_pnl | return_pct"
        )
        for r in self.rows:
            cp = r.get("current_price")
            mv = r.get("market_value")
            up = r.get("unrealized_pnl")
            rp = r.get("return_pct")
            lines.append(
                f"{r.get('symbol')} | {r.get('qty')} | {r.get('avg_cost')} | "
                f"{cp if cp is not None else '-'} | "
                f"{mv if mv is not None else '-'} | "
                f"{up if up is not None else '-'} | "
                f"{f'{rp:.2f}%' if isinstance(rp, (int, float)) else '-'}"
            )
        return "\n".join(lines)


class GetPortfolioTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "portfolio_id": {
                    "type": "string",
                    "description": "선택. 특정 포트폴리오 id. 미지정 시 primary 사용.",
                },
                "include_all": {
                    "type": "boolean",
                    "description": "true 면 사용자의 모든 포트폴리오 합산 보유 종목을 반환.",
                },
            },
            "required": [],
        }


def _supabase_get(path: str) -> Any:
    base_url = require_env("NEXT_PUBLIC_SUPABASE_URL").rstrip("/")
    key = require_env("SUPABASE_SERVICE_ROLE_KEY")
    request = Request(
        f"{base_url}/rest/v1/{path}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"supabase {path} failed: HTTP {exc.code} {details}") from exc


def _fetch_portfolios(user_id: str, portfolio_id: str, include_all: bool) -> list[dict]:
    if portfolio_id:
        rows = _supabase_get(
            f"trading_portfolios?id=eq.{portfolio_id}&user_id=eq.{user_id}&select=*"
        )
        return rows or []
    if include_all:
        rows = _supabase_get(
            f"trading_portfolios?user_id=eq.{user_id}&select=*&order=created_at.asc"
        )
        return rows or []
    rows = _supabase_get(
        f"trading_portfolios?user_id=eq.{user_id}&is_primary=eq.true&select=*"
    )
    if rows:
        return rows
    rows = _supabase_get(
        f"trading_portfolios?user_id=eq.{user_id}&select=*&order=created_at.asc&limit=1"
    )
    return rows or []


def _fetch_positions(portfolio_ids: list[str]) -> list[dict]:
    if not portfolio_ids:
        return []
    ids = ",".join(portfolio_ids)
    rows = _supabase_get(
        f"trading_positions?portfolio_id=in.({ids})&select=*"
    )
    return rows or []


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _attach_quotes(rows: list[dict]) -> None:
    symbols = sorted({r["symbol"] for r in rows})
    if not symbols:
        return
    quotes: dict[str, dict] = {}

    def fetch(sym: str) -> tuple[str, dict | None]:
        try:
            return sym, kis_quote(sym)
        except Exception:
            return sym, None

    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as ex:
        for sym, q in ex.map(fetch, symbols):
            if q is not None:
                quotes[sym] = q

    for r in rows:
        q = quotes.get(r["symbol"])
        try:
            qty = int(r.get("qty") or 0)
            avg = float(r.get("avg_cost") or 0)
        except (TypeError, ValueError):
            qty = 0
            avg = 0.0
        if q:
            price = int(q["price"])
            r["current_price"] = price
            r["name"] = q.get("name")
            mv = price * qty
            r["market_value"] = mv
            r["unrealized_pnl"] = int(round((price - avg) * qty))
            r["return_pct"] = ((price / avg) - 1.0) * 100.0 if avg > 0 else None
        else:
            r["current_price"] = None
            r["market_value"] = None
            r["unrealized_pnl"] = None
            r["return_pct"] = None


def make_get_portfolio_tool() -> GetPortfolioTool:
    def _execute(action: GetPortfolioAction, conversation: object | None = None) -> GetPortfolioObservation:
        state = getattr(conversation, "state", None)
        user_id = ""
        session_id = ""
        if state is not None:
            user_id = str(state.get_agent_state("user_id", "") or "").strip()
            session_id = str(state.get_agent_state("session_id", "") or "").strip()

        if not user_id:
            return GetPortfolioObservation(
                content=[],
                user_id="",
                session_id=session_id,
                message="포트폴리오 조회는 로그인된 사용자만 사용할 수 있습니다.",
            )
        if not _is_uuid(user_id):
            return GetPortfolioObservation(
                content=[],
                user_id=user_id,
                session_id=session_id,
                message="유효하지 않은 사용자 식별자입니다.",
            )

        portfolios = _fetch_portfolios(
            user_id=user_id,
            portfolio_id=action.portfolio_id.strip(),
            include_all=bool(action.include_all),
        )
        if not portfolios:
            return GetPortfolioObservation(
                content=[],
                user_id=user_id,
                session_id=session_id,
                portfolios=[],
                rows=[],
                message=(
                    "포트폴리오가 없습니다. 사이드바 → 포트폴리오 → + 새 포트폴리오 에서 생성하거나, "
                    "에이전트에게 매수를 지시하면 자동으로 '내 포트폴리오'가 만들어집니다."
                ),
            )

        positions = _fetch_positions([p["id"] for p in portfolios])
        portfolio_meta = {p["id"]: p for p in portfolios}
        for r in positions:
            pf = portfolio_meta.get(r.get("portfolio_id"))
            if pf:
                r["portfolio_name"] = pf.get("name")
        _attach_quotes(positions)

        return GetPortfolioObservation(
            content=[],
            user_id=user_id,
            session_id=session_id,
            portfolios=portfolios,
            rows=positions,
        )

    return GetPortfolioTool(
        name="get_portfolio",
        description=(
            "현재 사용자의 페이퍼 트레이딩 포트폴리오 보유 종목을 조회합니다. portfolio_id 미지정 시 "
            "primary 포트폴리오를 사용하며, include_all=true 면 모든 포트폴리오를 합쳐서 반환합니다. "
            "각 종목에 KIS 현재가 · 평가금액 · 평가손익 · 수익률을 포함합니다."
        ),
        action_type=GetPortfolioAction,
        observation_type=GetPortfolioObservation,
        executor=_execute,
    )
