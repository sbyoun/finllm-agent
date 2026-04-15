"""register_job tool: register a scheduled analysis job for the user."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.env import require_env
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


def _parse_next_run(cron_expression: str) -> str:
    """Compute next run time from a cron expression. Simple implementation."""
    try:
        # Try using croniter if available
        from croniter import croniter  # type: ignore[import-untyped]
        cron = croniter(cron_expression, datetime.now(timezone.utc))
        return cron.get_next(datetime).isoformat()
    except ImportError:
        pass

    # Fallback: parse simple patterns
    # "0 9 * * *" -> next 9:00 UTC
    parts = cron_expression.strip().split()
    if len(parts) == 5:
        minute, hour = parts[0], parts[1]
        if minute.isdigit() and hour.isdigit():
            now = datetime.now(timezone.utc)
            target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if target <= now:
                from datetime import timedelta
                target += timedelta(days=1)
            return target.isoformat()

    # Default: 1 hour from now
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


@dataclass(slots=True)
class RegisterJobAction(Action):
    question: str = ""
    cron_expression: str = ""
    enabled: bool = True
    trade_specs: list[dict] | None = None
    on_failure: str = "skip"          # skip | retry_next_run
    max_runs: int | None = None       # None = 무기한, 1 = 1회성
    price_max: int | None = None      # 매수 상한가 (KRW)
    price_min: int | None = None      # 매도 하한가 (KRW)
    nonce: str | None = None          # required for trade jobs

    def to_arguments_json(self) -> str:
        return json.dumps({
            "question": self.question,
            "cron_expression": self.cron_expression,
            "enabled": self.enabled,
            "trade_specs": self.trade_specs,
            "on_failure": self.on_failure,
            "max_runs": self.max_runs,
            "price_max": self.price_max,
            "price_min": self.price_min,
            "nonce": self.nonce,
        }, ensure_ascii=False)


TRADE_SPEC_OPEN = "[TRADE_SPEC]"
TRADE_SPEC_CLOSE = "[/TRADE_SPEC]"


def _encode_trade_spec(action: RegisterJobAction) -> str:
    if not action.trade_specs:
        return ""
    spec = {
        "orders": action.trade_specs,
        "on_failure": action.on_failure or "skip",
        "max_runs": action.max_runs,
        "price_max": action.price_max,
        "price_min": action.price_min,
    }
    return f"\n\n{TRADE_SPEC_OPEN}{json.dumps(spec, ensure_ascii=False)}{TRADE_SPEC_CLOSE}"


def parse_trade_spec(question: str) -> dict | None:
    if not question or TRADE_SPEC_OPEN not in question:
        return None
    try:
        start = question.index(TRADE_SPEC_OPEN) + len(TRADE_SPEC_OPEN)
        end = question.index(TRADE_SPEC_CLOSE, start)
        return json.loads(question[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


@dataclass(slots=True)
class RegisterJobObservation(Observation):
    success: bool = False
    message: str = ""
    job_id: str | None = None

    def to_text(self) -> str:
        if self.success:
            return f"Job registered successfully (id={self.job_id}). {self.message}"
        return f"Job registration failed: {self.message}"


def _execute(action: RegisterJobAction, conversation: Any) -> RegisterJobObservation:
    state = conversation.state

    # 1. Check user authentication
    user_id = state.get_agent_state("user_id")
    if not user_id:
        return RegisterJobObservation(
            success=False,
            message="스케줄 알림은 회원 전용 기능입니다. 가입 후 이용해 주세요.",
        )

    # 2. Check telegram connection
    try:
        telegram_data = _supabase_request(
            f"telegram_connections?user_id=eq.{user_id}&select=telegram_chat_id"
        )
        if not telegram_data:
            return RegisterJobObservation(
                success=False,
                message="텔레그램 연결이 필요합니다. 설정 페이지에서 텔레그램을 먼저 연결해 주세요.",
            )
    except Exception as exc:
        return RegisterJobObservation(
            success=False,
            message=f"텔레그램 연결 상태를 확인하지 못했습니다: {exc}",
        )

    # 3. Validate cron expression
    cron = action.cron_expression.strip()
    parts = cron.split()
    if len(parts) != 5:
        return RegisterJobObservation(
            success=False,
            message=f"잘못된 cron 표현식입니다: '{cron}'. 5개 필드(분 시 일 월 요일)가 필요합니다.",
        )

    # 4. Compute next_run_at
    next_run_at = _parse_next_run(cron)

    # 4.5 Validate trade_specs if present
    consumed_nonce_row: dict | None = None
    if action.trade_specs:
        for o in action.trade_specs:
            if not isinstance(o, dict):
                return RegisterJobObservation(success=False, message="trade_specs 항목은 객체여야 합니다.")
            if not o.get("symbol") or o.get("side") not in ("buy", "sell"):
                return RegisterJobObservation(success=False, message=f"trade_specs 항목이 불완전합니다: {o}")
            try:
                if int(o.get("qty", 0)) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return RegisterJobObservation(success=False, message=f"trade_specs qty 가 1 이상 정수여야 합니다: {o}")

        # Nonce required for trade jobs.
        if not action.nonce:
            return RegisterJobObservation(
                success=False,
                message=(
                    "STOP. 거래 스케줄 잡은 prepare_trade 로 발급받은 nonce 가 필요합니다. "
                    "prepare_trade(mode='scheduled', ...) 호출 → 사용자 UI 승인 → nonce 첨부 후 register_job 호출. "
                    "자연어 '예/네' 는 승인으로 간주되지 않습니다."
                ),
            )
        # Build spec_hash: support only single-order trade jobs in MVP.
        if len(action.trade_specs) != 1:
            return RegisterJobObservation(
                success=False,
                message="현재는 단일 주문 스케줄 잡만 지원합니다 (trade_specs 길이 1).",
            )
        first = action.trade_specs[0]
        spec_for_hash: dict[str, Any] = {
            "symbol": str(first.get("symbol", "")).zfill(6),
            "side": str(first.get("side", "")).lower(),
            "qty": int(first.get("qty") or 0),
        }
        if action.price_max is not None:
            spec_for_hash["price_max"] = int(action.price_max)
        if action.price_min is not None:
            spec_for_hash["price_min"] = int(action.price_min)
        if cron:
            spec_for_hash["cron"] = cron
        if action.max_runs is not None:
            spec_for_hash["max_runs"] = int(action.max_runs)
        if action.on_failure:
            spec_for_hash["on_failure"] = action.on_failure

        from agent_runtime.tool.trading.prepare_trade import hash_spec as _hash_spec
        from agent_runtime.tool.trading.place_trade import consume_nonce as _consume_nonce
        spec_hash = _hash_spec(spec_for_hash)
        consumed_nonce_row = _consume_nonce(action.nonce, user_id, spec_hash)
        if not consumed_nonce_row:
            return RegisterJobObservation(
                success=False,
                message=(
                    "STOP. 유효하지 않거나 만료된 nonce 입니다. prepare_trade 로 새 nonce 를 발급받아 재시도하세요. "
                    "(spec drift / 미승인 / 만료 중 하나)"
                ),
            )

    # 5. Insert job
    persisted_question = action.question + _encode_trade_spec(action)
    try:
        result = _supabase_request(
            "scheduled_jobs",
            method="POST",
            body={
                "user_id": user_id,
                "question": persisted_question,
                "cron_expression": cron,
                "model_selection_id": state.get_agent_state("model_selection_id"),
                "enabled": action.enabled,
                "next_run_at": next_run_at,
            },
        )
        job_id = result[0]["id"] if isinstance(result, list) and result else None
        if consumed_nonce_row and job_id:
            try:
                _supabase_request(
                    f"trade_nonces?nonce=eq.{consumed_nonce_row['nonce']}",
                    method="PATCH",
                    body={"consumed_by_run": job_id},
                )
            except Exception:
                pass
        return RegisterJobObservation(
            success=True,
            message=f"스케줄이 등록되었습니다. 다음 실행: {next_run_at[:16].replace('T', ' ')} UTC",
            job_id=job_id,
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return RegisterJobObservation(
            success=False,
            message=f"잡 등록에 실패했습니다: {body[:200]}",
        )
    except Exception as exc:
        return RegisterJobObservation(
            success=False,
            message=f"잡 등록에 실패했습니다: {exc}",
        )


@dataclass(slots=True)
class RegisterJobTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The analysis question to run on schedule (e.g., '삼성전자 최근 실적 체크'). For trade jobs, summarize the order in plain Korean — the structured spec goes in trade_specs.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron schedule expression (5 fields: minute hour day month weekday). Examples: '0 9 * * *' = every day 9AM, '0 8 * * 1' = every Monday 8AM, '0 18 * * 1-5' = weekdays 6PM",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the job is active (default true)",
                },
                "trade_specs": {
                    "type": "array",
                    "description": "REQUIRED for any scheduled trade. Each item is a confirmed paper-trading order. Set ONLY after the user has explicitly confirmed every field via the slot dialog (symbol/side/qty/cron/price constraints/failure policy/expiration). Never invent values.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string", "description": "6-digit KR ticker"},
                            "side": {"type": "string", "enum": ["buy", "sell"]},
                            "qty": {"type": "integer", "minimum": 1},
                        },
                        "required": ["symbol", "side", "qty"],
                    },
                },
                "on_failure": {
                    "type": "string",
                    "enum": ["skip", "retry_next_run"],
                    "description": "Trade-job failure policy. 'skip' = log and continue; 'retry_next_run' = try again on next cron tick.",
                },
                "max_runs": {
                    "type": "integer",
                    "description": "Trade-job expiration. 1 = one-shot, omit = open-ended.",
                },
                "price_max": {
                    "type": "integer",
                    "description": "Buy-side price ceiling in KRW. If KIS quote exceeds this at execution time, the trade is skipped per on_failure.",
                },
                "price_min": {
                    "type": "integer",
                    "description": "Sell-side price floor in KRW.",
                },
                "nonce": {
                    "type": "string",
                    "description": "trade_specs 가 있는 경우 필수. prepare_trade(mode='scheduled') 로 발급받아 사용자 UI 승인을 거친 nonce.",
                },
            },
            "required": ["question", "cron_expression"],
        }


def make_register_job_tool() -> RegisterJobTool:
    return RegisterJobTool(
        name="register_job",
        description=(
            "Register a scheduled analysis job. The job will run automatically at the specified schedule "
            "and send results to the user's connected Telegram. "
            "Requires: authenticated user with Telegram connected. "
            "Convert natural language schedule to cron expression (e.g., '매일 아침 9시' → '0 9 * * *', '매주 월요일 오전 8시' → '0 8 * * 1')."
        ),
        action_type=RegisterJobAction,
        observation_type=RegisterJobObservation,
        executor=_execute,
    )
