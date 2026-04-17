"""create_forward_test tool: create a forward test from a backtest or LLM strategy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _parse_next_run(cron_expression: str) -> str:
    try:
        from croniter import croniter  # type: ignore[import-untyped]
        cron = croniter(cron_expression, datetime.now(timezone.utc))
        return cron.get_next(datetime).isoformat()
    except ImportError:
        pass
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
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


@dataclass(slots=True)
class CreateForwardTestAction(Action):
    name: str = ""
    strategy_type: str = "llm"          # "sql" | "llm"
    screening_sql: str | None = None    # for strategy_type="sql"
    strategy_prompt: str | None = None  # for strategy_type="llm"
    backtest_result_id: str | None = None
    universe: str = "KOSPI"
    rebalance: str = "quarterly"
    cron_expression: str = ""
    initial_capital: float = 100_000_000

    def to_arguments_json(self) -> str:
        d = {
            "name": self.name,
            "strategy_type": self.strategy_type,
            "universe": self.universe,
            "rebalance": self.rebalance,
            "cron_expression": self.cron_expression,
            "initial_capital": self.initial_capital,
        }
        if self.screening_sql:
            d["screening_sql"] = self.screening_sql
        if self.strategy_prompt:
            d["strategy_prompt"] = self.strategy_prompt
        if self.backtest_result_id:
            d["backtest_result_id"] = self.backtest_result_id
        return json.dumps(d, ensure_ascii=False)


@dataclass(slots=True)
class CreateForwardTestObservation(Observation):
    success: bool = False
    message: str = ""
    forward_test_id: str | None = None
    job_id: str | None = None

    def to_text(self) -> str:
        if self.success:
            return (
                f"포워드 테스트가 생성되었습니다.\n"
                f"- ID: {self.forward_test_id}\n"
                f"- 스케줄 잡 ID: {self.job_id}\n"
                f"{self.message}"
            )
        return f"포워드 테스트 생성 실패: {self.message}"


def _execute(action: CreateForwardTestAction, conversation: Any) -> CreateForwardTestObservation:
    state = conversation.state

    user_id = state.get_agent_state("user_id")
    if not user_id:
        return CreateForwardTestObservation(
            success=False,
            message="회원 전용 기능입니다. 가입 후 이용해 주세요.",
        )

    # Validate strategy
    if action.strategy_type == "sql" and not action.screening_sql:
        return CreateForwardTestObservation(
            success=False,
            message="SQL 기반 전략에는 screening_sql이 필요합니다.",
        )
    if action.strategy_type == "llm" and not action.strategy_prompt:
        return CreateForwardTestObservation(
            success=False,
            message="LLM 기반 전략에는 strategy_prompt가 필요합니다.",
        )

    # Validate cron
    cron = action.cron_expression.strip()
    parts = cron.split()
    if len(parts) != 5:
        return CreateForwardTestObservation(
            success=False,
            message=f"잘못된 cron 표현식: '{cron}'. 5개 필드(분 시 일 월 요일)가 필요합니다.",
        )

    # Check telegram connection
    try:
        telegram_data = _supabase_request(
            f"telegram_connections?user_id=eq.{user_id}&select=telegram_chat_id"
        )
        if not telegram_data:
            return CreateForwardTestObservation(
                success=False,
                message="텔레그램 연결이 필요합니다. 설정 페이지에서 텔레그램을 먼저 연결해 주세요.",
            )
    except Exception as exc:
        return CreateForwardTestObservation(
            success=False,
            message=f"텔레그램 연결 확인 실패: {exc}",
        )

    # Build the job question prompt based on strategy type
    if action.strategy_type == "sql":
        job_question = (
            f"[포워드 테스트 리밸런싱] {action.name}\n"
            f"strategy_type: sql\n"
            f"screening_sql: {action.screening_sql}\n"
            f"universe: {action.universe}\n"
            f"screening_sql을 오늘 날짜로 실행하여 종목을 선정하고, "
            f"현재가를 조회한 뒤 균등 배분으로 리밸런싱하세요. "
            f"완료 후 save_forward_snapshot을 호출하세요."
        )
    else:
        job_question = (
            f"[포워드 테스트 실행] {action.name}\n"
            f"strategy_type: llm\n"
            f"전략: {action.strategy_prompt}\n"
            f"universe: {action.universe}\n"
            f"전략에 따라 오늘 매매할 종목을 판단하고 실행하세요. "
            f"완료 후 save_forward_snapshot을 호출하세요."
        )

    next_run_at = _parse_next_run(cron)

    try:
        # 1. Create scheduled job
        job_result = _supabase_request(
            "scheduled_jobs",
            method="POST",
            body={
                "user_id": user_id,
                "question": job_question,
                "cron_expression": cron,
                "model_selection_id": state.get_agent_state("model_selection_id"),
                "enabled": True,
                "next_run_at": next_run_at,
            },
        )
        job_id = job_result[0]["id"] if isinstance(job_result, list) and job_result else None
        if not job_id:
            return CreateForwardTestObservation(
                success=False,
                message="스케줄 잡 생성에 실패했습니다.",
            )

        # 2. Create forward test
        ft_body: dict[str, Any] = {
            "user_id": user_id,
            "job_id": job_id,
            "name": action.name,
            "strategy_type": action.strategy_type,
            "universe": action.universe,
            "rebalance": action.rebalance,
            "initial_capital": action.initial_capital,
        }
        if action.screening_sql:
            ft_body["screening_sql"] = action.screening_sql
        if action.strategy_prompt:
            ft_body["strategy_prompt"] = action.strategy_prompt
        if action.backtest_result_id:
            ft_body["backtest_result_id"] = action.backtest_result_id

        ft_result = _supabase_request("forward_tests", method="POST", body=ft_body)
        ft_id = ft_result[0]["id"] if isinstance(ft_result, list) and ft_result else None

        return CreateForwardTestObservation(
            success=True,
            message=f"다음 실행: {next_run_at[:16].replace('T', ' ')} UTC",
            forward_test_id=ft_id,
            job_id=job_id,
        )

    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return CreateForwardTestObservation(
            success=False, message=f"생성 실패: {body[:200]}"
        )
    except Exception as exc:
        return CreateForwardTestObservation(
            success=False, message=f"생성 실패: {exc}"
        )


@dataclass(slots=True)
class CreateForwardTestTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "포워드 테스트 이름 (예: '저PER 고성장 전략', '뉴스 기반 데이트레이딩')",
                },
                "strategy_type": {
                    "type": "string",
                    "enum": ["sql", "llm"],
                    "description": "sql: 백테스트의 screening_sql 재사용. llm: 자연어 전략을 에이전트가 해석.",
                },
                "screening_sql": {
                    "type": "string",
                    "description": "strategy_type='sql'일 때 필수. {as_of_date} 플레이스홀더 포함 SQL.",
                },
                "strategy_prompt": {
                    "type": "string",
                    "description": "strategy_type='llm'일 때 필수. 자연어 전략 설명.",
                },
                "backtest_result_id": {
                    "type": "string",
                    "description": "연결할 백테스트 결과 ID (선택)",
                },
                "universe": {
                    "type": "string",
                    "enum": ["KOSPI", "KOSDAQ", "SP500", "NASDAQ"],
                    "description": "투자 유니버스 (기본: KOSPI)",
                },
                "rebalance": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly", "quarterly"],
                    "description": "리밸런싱 주기 (기본: quarterly)",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "실행 스케줄 cron (5필드). 예: '0 0 * * 1-5' = 평일 09:00 KST",
                },
                "initial_capital": {
                    "type": "number",
                    "description": "초기 자본금 (기본: 1억원)",
                },
            },
            "required": ["name", "strategy_type", "cron_expression"],
        }


def make_create_forward_test_tool() -> CreateForwardTestTool:
    return CreateForwardTestTool(
        name="create_forward_test",
        description=(
            "포워드 테스트를 생성합니다. 전략을 정의하고 스케줄을 설정하면, "
            "에이전트가 자동으로 페이퍼 매매를 실행합니다. "
            "SQL 기반(백테스트 screening_sql 재사용) 또는 LLM 기반(자연어 전략)을 선택할 수 있습니다."
        ),
        action_type=CreateForwardTestAction,
        observation_type=CreateForwardTestObservation,
        executor=_execute,
    )
