"""prepare_trade tool: issue a one-time nonce for a trade spec that requires
user approval via UI button (web modal / telegram inline button).

The LLM cannot call ``place_trade`` / ``register_job`` (for trade jobs) without
first obtaining an ``approved`` nonce from this tool. Natural-language "yes"
has no effect — only UI button clicks mutate the nonce status.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
BACKEND_URL = os.getenv("FOLDALPHA_BACKEND_URL", "").strip() or os.getenv("BACKEND_URL", "").strip()


def _supabase_insert(path: str, body: dict) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    req = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def canonical_spec(spec: dict) -> dict:
    """Normalize a trade spec so the same logical order always hashes identically."""
    out: dict[str, Any] = {}
    symbol = str(spec.get("symbol", "")).strip()
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    out["symbol"] = symbol
    out["side"] = str(spec.get("side", "")).lower()
    try:
        out["qty"] = int(spec.get("qty", 0))
    except (TypeError, ValueError):
        out["qty"] = 0
    for k in ("price_max", "price_min", "cron", "max_runs", "on_failure"):
        v = spec.get(k)
        if v is not None and v != "":
            out[k] = v
    return out


def hash_spec(spec: dict) -> str:
    canonical = canonical_spec(spec)
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lookup_name(symbol: str) -> str:
    try:
        from agent_runtime.tool.sql.oracle import OracleSQLRunner
        runner = OracleSQLRunner()
        _, rows = runner(f"SELECT name FROM stocks WHERE ticker = '{symbol}' AND ROWNUM = 1")
        if rows:
            return str(rows[0].get("name") or "")
    except Exception:
        pass
    return ""


def _preview_text(canonical: dict) -> str:
    name = _lookup_name(canonical["symbol"]) if canonical.get("symbol") else ""
    label = f"{name}({canonical['symbol']})" if name else canonical.get("symbol", "?")
    side_kr = "매수" if canonical.get("side") == "buy" else "매도"
    qty = canonical.get("qty", 0)
    parts = [f"{label} {qty}주 {side_kr}"]
    if canonical.get("price_max"):
        parts.append(f"상한가 {canonical['price_max']:,}원")
    if canonical.get("price_min"):
        parts.append(f"하한가 {canonical['price_min']:,}원")
    if canonical.get("cron"):
        parts.append(f"cron={canonical['cron']}")
    if canonical.get("max_runs"):
        parts.append(f"max_runs={canonical['max_runs']}")
    return " · ".join(parts)


def _notify_backend(nonce: str, user_id: str, preview_text: str, session_id: str | None) -> str:
    """Best-effort: ping backend to send Telegram inline-button prompt.

    Returns channel hint. Backend route is not yet implemented (§5 후속 세션),
    so we attempt the call but treat connection failure as non-fatal.
    """
    if not BACKEND_URL:
        return "web-modal"
    try:
        body = json.dumps({
            "nonce": nonce,
            "user_id": user_id,
            "preview_text": preview_text,
            "session_id": session_id,
        }).encode()
        req = Request(
            f"{BACKEND_URL.rstrip('/')}/api/trade/prompt",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Service-Key": os.getenv("BACKEND_INTERNAL_KEY", ""),
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            raw = resp.read() or b"{}"
            data = json.loads(raw)
            if data.get("telegram_sent"):
                return "web-modal + telegram-inline"
    except Exception:
        # TODO(후속 세션 §5): backend route 구현 후 실패시 로깅
        pass
    return "web-modal"


@dataclass(slots=True)
class PrepareTradeAction(Action):
    symbol: str = ""
    side: str = ""
    qty: int = 0
    mode: str = "immediate"  # immediate | scheduled
    price_max: int | None = None
    price_min: int | None = None
    cron: str | None = None
    max_runs: int | None = None
    on_failure: str | None = None

    def to_arguments_json(self) -> str:
        return json.dumps({
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "mode": self.mode,
            "price_max": self.price_max,
            "price_min": self.price_min,
            "cron": self.cron,
            "max_runs": self.max_runs,
            "on_failure": self.on_failure,
        }, ensure_ascii=False)


@dataclass(slots=True)
class PrepareTradeObservation(Observation):
    success: bool = False
    nonce: str | None = None
    expires_at: str | None = None
    preview_text: str = ""
    approval_channel: str = ""
    mode: str = ""
    message: str = ""

    def to_text(self) -> str:
        if not self.success:
            return f"prepare_trade 실패: {self.message}"
        return (
            f"[승인 대기] {self.preview_text}\n"
            f"만료: {self.expires_at}\n"
            f"승인 채널: {self.approval_channel}\n"
            f"사용자에게 위 내용을 보여주고, '웹 모달 또는 텔레그램 버튼으로 승인해주세요'라고 안내하세요. "
            f"사용자가 자연어로 '예/네'라고만 응답하면 절대 place_trade/register_job을 호출하지 마세요. "
            f"UI 버튼 클릭 후에만 nonce가 approved 상태가 되며, 다음 턴에서 nonce='{self.nonce}' 를 "
            f"첨부해 place_trade 또는 register_job을 호출할 수 있습니다."
        )


def _execute(action: PrepareTradeAction, conversation: Any) -> PrepareTradeObservation:
    state = conversation.state if conversation is not None else None
    user_id = state.get_agent_state("user_id") if state else None
    if not user_id:
        return PrepareTradeObservation(success=False, message="로그인이 필요합니다.")

    session_id = state.get_agent_state("session_id") if state else None

    mode = (action.mode or "immediate").strip().lower()
    if mode not in ("immediate", "scheduled"):
        return PrepareTradeObservation(success=False, message=f"mode는 immediate|scheduled 이어야 합니다: {mode!r}")

    symbol = (action.symbol or "").strip()
    if not symbol:
        return PrepareTradeObservation(success=False, message="symbol 이 필요합니다.")
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    if len(symbol) != 6 or not symbol.isdigit():
        return PrepareTradeObservation(success=False, message=f"6자리 KR 티커가 필요합니다: {symbol!r}")

    side = (action.side or "").strip().lower()
    if side not in ("buy", "sell"):
        return PrepareTradeObservation(success=False, message="side 는 buy|sell 이어야 합니다.")

    try:
        qty = int(action.qty)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return PrepareTradeObservation(success=False, message="qty 는 1 이상 정수여야 합니다.")

    spec: dict[str, Any] = {"symbol": symbol, "side": side, "qty": qty}
    if action.price_max is not None:
        spec["price_max"] = int(action.price_max)
    if action.price_min is not None:
        spec["price_min"] = int(action.price_min)
    if action.cron:
        spec["cron"] = action.cron.strip()
    if action.max_runs is not None:
        spec["max_runs"] = int(action.max_runs)
    if action.on_failure:
        spec["on_failure"] = action.on_failure.strip()

    canonical = canonical_spec(spec)
    spec_hash = hash_spec(spec)
    nonce = secrets.token_urlsafe(32)

    ttl = timedelta(minutes=5) if mode == "immediate" else timedelta(days=1)
    now = datetime.now(timezone.utc)
    expires_at = (now + ttl).isoformat()

    try:
        _supabase_insert(
            "trade_nonces",
            {
                "nonce": nonce,
                "user_id": user_id,
                "spec_hash": spec_hash,
                "spec_json": canonical,
                "mode": mode,
                "status": "pending",
                "channel": "web",
                "session_id": session_id,
                "expires_at": expires_at,
            },
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return PrepareTradeObservation(success=False, message=f"nonce 발급 실패: HTTP {exc.code} {body[:200]}")
    except Exception as exc:  # noqa: BLE001
        return PrepareTradeObservation(success=False, message=f"nonce 발급 실패: {exc}")

    preview = _preview_text(canonical)
    channel = _notify_backend(nonce, user_id, preview, session_id if isinstance(session_id, str) else None)

    return PrepareTradeObservation(
        success=True,
        nonce=nonce,
        expires_at=expires_at,
        preview_text=preview,
        approval_channel=channel,
        mode=mode,
        message="",
    )


@dataclass(slots=True)
class PrepareTradeTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "6-digit KR ticker"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "qty": {"type": "integer", "minimum": 1},
                "mode": {
                    "type": "string",
                    "enum": ["immediate", "scheduled"],
                    "description": "immediate = place_trade 직후 집행 (TTL 5분). scheduled = register_job 로 스케줄 잡 등록 (TTL 1일).",
                },
                "price_max": {"type": "integer", "description": "매수 상한가 (KRW). scheduled 모드에서 사용."},
                "price_min": {"type": "integer", "description": "매도 하한가 (KRW). scheduled 모드에서 사용."},
                "cron": {"type": "string", "description": "cron expression. scheduled 모드에서 필수."},
                "max_runs": {"type": "integer", "description": "1=1회성, omit=무기한. scheduled 모드."},
                "on_failure": {"type": "string", "enum": ["skip", "retry_next_run"]},
            },
            "required": ["symbol", "side", "qty", "mode"],
        }


def make_prepare_trade_tool() -> PrepareTradeTool:
    return PrepareTradeTool(
        name="prepare_trade",
        description=(
            "거래 의도가 확정된 직후 호출하여 승인용 nonce 를 발급합니다. "
            "모든 slot(symbol/side/qty + scheduled 인 경우 cron/max_runs/price 제약)이 확정된 후에만 호출. "
            "발급된 nonce 는 pending 상태이며, 사용자가 웹 모달 또는 텔레그램 inline button 으로 승인해야 approved 로 전환됩니다. "
            "자연어 '예/네' 는 승인으로 간주되지 않습니다. 승인 후 다음 턴에서 nonce 를 첨부해 place_trade (immediate) "
            "또는 register_job (scheduled) 을 호출하세요."
        ),
        action_type=PrepareTradeAction,
        observation_type=PrepareTradeObservation,
        executor=_execute,
    )
