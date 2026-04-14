"""place_trade tool: paper-trading buy/sell against Supabase trading_* tables.

Phase A: paper trading only. Records trades into ``trading_trades`` and
maintains ``trading_positions`` weighted average cost. KIS live execution is
out of scope (Phase B).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_runtime.kis.quote import kis_quote
from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("FOLDALPHA_TELEGRAM_BOT_TOKEN", "").strip()

BUY_FEE_RATE = 0.00015   # 0.015% 위탁수수료
SELL_FEE_RATE = 0.00195  # 0.015% + 0.18% 거래세


def _supabase_request(
    path: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    prefer: str = "return=representation",
) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": prefer,
    }
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=15) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw)


def _get_or_create_portfolio(user_id: str, portfolio_id: str | None) -> dict:
    if portfolio_id:
        rows = _supabase_request(
            f"trading_portfolios?id=eq.{portfolio_id}&select=*"
        )
        if not rows:
            raise RuntimeError(f"포트폴리오를 찾을 수 없습니다: {portfolio_id}")
        if rows[0].get("user_id") != user_id:
            raise RuntimeError("해당 포트폴리오에 접근 권한이 없습니다.")
        return rows[0]

    rows = _supabase_request(
        f"trading_portfolios?user_id=eq.{user_id}&is_primary=eq.true&select=*"
    )
    if rows:
        return rows[0]

    # Auto-create primary portfolio
    created = _supabase_request(
        "trading_portfolios",
        method="POST",
        body={
            "user_id": user_id,
            "name": "내 포트폴리오",
            "is_primary": True,
        },
    )
    if not created:
        raise RuntimeError("포트폴리오 자동 생성 실패")
    return created[0] if isinstance(created, list) else created


def _send_telegram(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def _notify_trade(user_id: str, portfolio_name: str, message: str) -> None:
    """Append to trading-{user_id} chat session and send Telegram if connected."""
    import uuid
    session_id = f"trading-{user_id}"
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        existing = _supabase_request(
            f"chat_sessions?user_id=eq.{user_id}&session_id=eq.{session_id}&select=session_id"
        )
        if not existing:
            _supabase_request(
                "chat_sessions",
                method="POST",
                body={
                    "user_id": user_id,
                    "session_id": session_id,
                    "title": "📈 페이퍼 트레이딩",
                    "result_json": {},
                    "updated_at": now_iso,
                },
                prefer="return=minimal",
            )
        _supabase_request(
            "chat_messages",
            method="POST",
            body={
                "user_id": user_id,
                "session_id": session_id,
                "message_id": f"assistant-{uuid.uuid4()}",
                "role": "assistant",
                "content": f"[{portfolio_name}]\n{message}",
                "created_at": now_iso,
            },
            prefer="return=minimal",
        )
        _supabase_request(
            f"chat_sessions?user_id=eq.{user_id}&session_id=eq.{session_id}",
            method="PATCH",
            body={"updated_at": now_iso},
            prefer="return=minimal",
        )
    except Exception:
        pass

    try:
        tg = _supabase_request(
            f"telegram_connections?user_id=eq.{user_id}&select=telegram_chat_id"
        )
        if tg:
            chat_id = tg[0].get("telegram_chat_id")
            if chat_id:
                _send_telegram(str(chat_id), f"[{portfolio_name}]\n{message}")
    except Exception:
        pass


def _get_position(portfolio_id: str, symbol: str) -> dict | None:
    rows = _supabase_request(
        f"trading_positions?portfolio_id=eq.{portfolio_id}&symbol=eq.{symbol}&select=*"
    )
    if rows:
        return rows[0]
    return None


@dataclass(slots=True)
class PlaceTradeAction(Action):
    symbol: str = ""
    side: str = ""
    qty: int = 0
    portfolio_id: str = ""

    def to_arguments_json(self) -> str:
        return json.dumps(
            {
                "symbol": self.symbol,
                "side": self.side,
                "qty": self.qty,
                "portfolio_id": self.portfolio_id or None,
            },
            ensure_ascii=False,
        )


@dataclass(slots=True)
class PlaceTradeObservation(Observation):
    success: bool = False
    message: str = ""
    portfolio_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    qty: int = 0
    price: int = 0
    fee: int = 0
    realized_pnl: int | None = None
    new_qty: int = 0
    new_avg_cost: float = 0.0

    def to_text(self) -> str:
        if not self.success:
            return f"place_trade 실패: {self.message}"
        side_kr = "매수" if self.side in ("buy", "manual_buy") else "매도"
        lines = [
            f"[페이퍼 체결] {self.symbol} {side_kr} {self.qty}주 @{self.price:,}원",
            f"수수료 {self.fee:,}원",
            f"보유 후: {self.new_qty}주 (평균단가 {self.new_avg_cost:,.2f}원)",
        ]
        if self.realized_pnl is not None:
            lines.append(f"실현손익 {self.realized_pnl:+,}원")
        if self.message:
            lines.append(self.message)
        return "\n".join(lines)


def _execute(action: PlaceTradeAction, conversation: Any) -> PlaceTradeObservation:
    state = conversation.state if conversation is not None else None
    user_id = state.get_agent_state("user_id") if state else None
    if not user_id:
        return PlaceTradeObservation(
            success=False,
            message="페이퍼 트레이딩은 회원 전용 기능입니다. 로그인 후 이용해 주세요.",
        )

    side = (action.side or "").strip().lower()
    if side not in ("buy", "sell"):
        return PlaceTradeObservation(
            success=False, message=f"side는 'buy' 또는 'sell' 이어야 합니다. (입력: {action.side!r})"
        )

    symbol = (action.symbol or "").strip()
    if not symbol:
        return PlaceTradeObservation(success=False, message="symbol 이 필요합니다.")
    symbol = symbol.zfill(6) if symbol.isdigit() else symbol

    try:
        qty = int(action.qty)
    except (TypeError, ValueError):
        return PlaceTradeObservation(success=False, message=f"qty 정수가 필요합니다: {action.qty!r}")
    if qty <= 0:
        return PlaceTradeObservation(success=False, message="qty 는 1 이상이어야 합니다.")

    # Portfolio
    try:
        portfolio = _get_or_create_portfolio(user_id, action.portfolio_id or None)
    except Exception as exc:  # noqa: BLE001
        return PlaceTradeObservation(success=False, message=f"포트폴리오 조회 실패: {exc}")
    portfolio_id = portfolio["id"]

    # Quote
    try:
        quote = kis_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        return PlaceTradeObservation(
            success=False, message=f"KIS 현재가 조회 실패 ({symbol}): {exc}"
        )
    price = int(quote["price"])

    gross = price * qty
    if side == "buy":
        fee = int(round(gross * BUY_FEE_RATE))
    else:
        fee = int(round(gross * SELL_FEE_RATE))

    existing = _get_position(portfolio_id, symbol)
    realized_pnl: int | None = None
    new_qty: int
    new_avg_cost: float

    if side == "buy":
        old_qty = int(existing["qty"]) if existing else 0
        old_avg = float(existing["avg_cost"]) if existing else 0.0
        new_qty = old_qty + qty
        new_avg_cost = (old_avg * old_qty + price * qty) / new_qty if new_qty else 0.0
    else:  # sell
        if not existing:
            return PlaceTradeObservation(
                success=False, message=f"보유 포지션이 없습니다: {symbol}"
            )
        old_qty = int(existing["qty"])
        old_avg = float(existing["avg_cost"])
        if qty > old_qty:
            return PlaceTradeObservation(
                success=False,
                message=f"보유 수량 부족: 보유 {old_qty}주, 요청 {qty}주",
            )
        realized_pnl = int(round((price - old_avg) * qty - fee))
        new_qty = old_qty - qty
        new_avg_cost = old_avg if new_qty > 0 else 0.0

    now_iso = datetime.now(timezone.utc).isoformat()

    # Position upsert/delete
    try:
        if side == "buy":
            if existing:
                _supabase_request(
                    f"trading_positions?portfolio_id=eq.{portfolio_id}&symbol=eq.{symbol}",
                    method="PATCH",
                    body={"qty": new_qty, "avg_cost": new_avg_cost, "updated_at": now_iso},
                )
            else:
                _supabase_request(
                    "trading_positions",
                    method="POST",
                    body={
                        "portfolio_id": portfolio_id,
                        "symbol": symbol,
                        "qty": new_qty,
                        "avg_cost": new_avg_cost,
                        "updated_at": now_iso,
                    },
                )
        else:
            if new_qty == 0:
                _supabase_request(
                    f"trading_positions?portfolio_id=eq.{portfolio_id}&symbol=eq.{symbol}",
                    method="DELETE",
                    prefer="return=minimal",
                )
            else:
                _supabase_request(
                    f"trading_positions?portfolio_id=eq.{portfolio_id}&symbol=eq.{symbol}",
                    method="PATCH",
                    body={"qty": new_qty, "updated_at": now_iso},
                )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return PlaceTradeObservation(
            success=False, message=f"포지션 갱신 실패: HTTP {exc.code} {body[:200]}"
        )
    except Exception as exc:  # noqa: BLE001
        return PlaceTradeObservation(success=False, message=f"포지션 갱신 실패: {exc}")

    # Always insert trade row
    try:
        chat_run_id = state.get_agent_state("chat_run_id") if state else None
        trade_row: dict[str, Any] = {
            "portfolio_id": portfolio_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "fee": fee,
            "executed_at": now_iso,
            "source": "agent",
        }
        if realized_pnl is not None:
            trade_row["realized_pnl"] = realized_pnl
        if chat_run_id:
            trade_row["chat_run_id"] = chat_run_id
        _supabase_request("trading_trades", method="POST", body=trade_row)
    except Exception as exc:  # noqa: BLE001
        # Trade row failure is logged but does not roll back position
        return PlaceTradeObservation(
            success=True,
            message=f"(경고) 포지션은 갱신됐지만 거래 로그 기록 실패: {exc}",
            portfolio_id=portfolio_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            fee=fee,
            realized_pnl=realized_pnl,
            new_qty=new_qty,
            new_avg_cost=new_avg_cost,
        )

    # Touch portfolio updated_at
    try:
        _supabase_request(
            f"trading_portfolios?id=eq.{portfolio_id}",
            method="PATCH",
            body={"updated_at": now_iso},
            prefer="return=minimal",
        )
    except Exception:
        pass

    # Notify (chat session + telegram, best-effort)
    obs_for_text = PlaceTradeObservation(
        success=True,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        realized_pnl=realized_pnl,
        new_qty=new_qty,
        new_avg_cost=new_avg_cost,
    )
    _notify_trade(user_id, portfolio.get("name") or "포트폴리오", obs_for_text.to_text())

    return PlaceTradeObservation(
        success=True,
        message="",
        portfolio_id=portfolio_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        realized_pnl=realized_pnl,
        new_qty=new_qty,
        new_avg_cost=new_avg_cost,
    )


@dataclass(slots=True)
class PlaceTradeTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "한국 주식 종목 코드 (예: '005930'). 6자리가 아니면 자동으로 0 패딩.",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "매수 'buy' 또는 매도 'sell'.",
                },
                "qty": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "거래 수량 (1 이상 정수).",
                },
                "portfolio_id": {
                    "type": "string",
                    "description": "선택. 미지정 시 사용자의 primary 포트폴리오를 사용하며, 없으면 자동 생성.",
                },
            },
            "required": ["symbol", "side", "qty"],
        }


def make_place_trade_tool() -> PlaceTradeTool:
    return PlaceTradeTool(
        name="place_trade",
        description=(
            "페이퍼 트레이딩 체결을 기록합니다. KIS 실시간 시세를 가져와 한국 수수료 모델을 적용한 뒤 "
            "Supabase trading_positions / trading_trades 에 반영합니다. portfolio_id 미지정 시 사용자의 "
            "primary 포트폴리오를 사용하며, 없으면 '내 포트폴리오' 이름으로 자동 생성합니다. "
            "매도 시 보유 수량을 검증하고 실현손익을 계산해 기록합니다. 호출 전 종목 선정 근거와 "
            "수량 산출 이유를 사용자에게 명시하세요."
        ),
        action_type=PlaceTradeAction,
        observation_type=PlaceTradeObservation,
        executor=_execute,
    )
