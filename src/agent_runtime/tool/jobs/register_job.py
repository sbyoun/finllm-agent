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

    def to_arguments_json(self) -> str:
        return json.dumps({
            "question": self.question,
            "cron_expression": self.cron_expression,
            "enabled": self.enabled,
        }, ensure_ascii=False)


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

    # 5. Insert job
    try:
        result = _supabase_request(
            "scheduled_jobs",
            method="POST",
            body={
                "user_id": user_id,
                "question": action.question,
                "cron_expression": cron,
                "model_selection_id": state.get_agent_state("model_selection_id"),
                "enabled": action.enabled,
                "next_run_at": next_run_at,
            },
        )
        job_id = result[0]["id"] if isinstance(result, list) and result else None
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
                    "description": "The analysis question to run on schedule (e.g., '삼성전자 최근 실적 체크')",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron schedule expression (5 fields: minute hour day month weekday). Examples: '0 9 * * *' = every day 9AM, '0 8 * * 1' = every Monday 8AM, '0 18 * * 1-5' = weekdays 6PM",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the job is active (default true)",
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
