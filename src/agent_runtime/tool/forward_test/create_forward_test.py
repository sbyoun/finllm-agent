"""create_forward_test tool: create a forward test from a backtest or LLM strategy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def _parse_cron_number_field(field: str, minimum: int, maximum: int) -> list[int] | None:
    if field == "*":
        return list(range(minimum, maximum + 1))

    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError:
                return None
            if start > end:
                return None
            for value in range(start, end + 1):
                if value < minimum or value > maximum:
                    return None
                values.add(value)
            continue
        try:
            value = int(part)
        except ValueError:
            return None
        if value < minimum or value > maximum:
            return None
        values.add(value)

    return sorted(values)


def _parse_cron_weekday_field(field: str) -> list[int] | None:
    values = _parse_cron_number_field(field, 0, 7)
    if values is None:
        return None
    return sorted({value % 7 for value in values})


def _parse_next_run_without_croniter(cron_expression: str, now: datetime) -> datetime:
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return now + timedelta(hours=1)

    minute_field, hour_field, day_field, _, weekday_field = parts
    if day_field != "*":
        return now + timedelta(hours=1)

    minutes = _parse_cron_number_field(minute_field, 0, 59)
    hours = _parse_cron_number_field(hour_field, 0, 23)
    weekdays = _parse_cron_weekday_field(weekday_field)
    if not minutes or not hours or weekdays == []:
        return now + timedelta(hours=1)

    for day_offset in range(8):
        for hour in hours:
            for minute in minutes:
                candidate = now + timedelta(days=day_offset)
                candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    continue
                cron_weekday = candidate.isoweekday() % 7
                if weekdays is not None and cron_weekday not in weekdays:
                    continue
                return candidate

    return now + timedelta(hours=1)


def _parse_next_run_datetime(cron_expression: str, now: datetime) -> datetime:
    try:
        from croniter import croniter  # type: ignore[import-untyped]
        cron = croniter(cron_expression, now)
        next_run = cron.get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        return next_run
    except ImportError:
        pass

    return _parse_next_run_without_croniter(cron_expression, now)


def _parse_next_run(cron_expression: str, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return _parse_next_run_datetime(cron_expression, base).isoformat()


def _parse_next_run_after(cron_expression: str, after: datetime) -> str:
    return _parse_next_run_datetime(cron_expression, after).isoformat()


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
    schedules: list[dict[str, Any]] | None = None
    initial_capital: float = 100_000_000

    def to_arguments_json(self) -> str:
        d = {
            "name": self.name,
            "strategy_type": self.strategy_type,
            "universe": self.universe,
            "rebalance": self.rebalance,
            "initial_capital": self.initial_capital,
        }
        if self.cron_expression:
            d["cron_expression"] = self.cron_expression
        if self.schedules:
            d["schedules"] = self.schedules
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
    job_ids: list[str] | None = None

    def to_text(self) -> str:
        if self.success:
            job_lines = ""
            if self.job_ids:
                job_lines = "\n".join(f"- 스케줄 잡 ID: {job_id}" for job_id in self.job_ids)
            elif self.job_id:
                job_lines = f"- 스케줄 잡 ID: {self.job_id}"
            return (
                f"포워드 테스트가 생성되었습니다.\n"
                f"- ID: {self.forward_test_id}\n"
                f"{job_lines}\n"
                f"{self.message}"
            )
        return f"포워드 테스트 생성 실패: {self.message}"


def _build_schedules(action: CreateForwardTestAction) -> list[dict[str, str]]:
    if action.schedules:
        schedules: list[dict[str, str]] = []
        for index, raw in enumerate(action.schedules, start=1):
            cron = str(raw.get("cron_expression", "")).strip()
            role = str(raw.get("role", f"step_{index}")).strip() or f"step_{index}"
            prompt = str(raw.get("prompt", "")).strip()
            if not cron:
                raise ValueError(f"schedules[{index}].cron_expression이 필요합니다.")
            if len(cron.split()) != 5:
                raise ValueError(f"잘못된 cron 표현식: '{cron}'. 5개 필드(분 시 일 월 요일)가 필요합니다.")
            schedules.append({"role": role, "cron_expression": cron, "prompt": prompt})
        return schedules

    cron = action.cron_expression.strip()
    if not cron:
        raise ValueError("cron_expression 또는 schedules가 필요합니다.")
    if len(cron.split()) != 5:
        raise ValueError(f"잘못된 cron 표현식: '{cron}'. 5개 필드(분 시 일 월 요일)가 필요합니다.")
    return [{"role": "rebalance", "cron_expression": cron, "prompt": ""}]


def _build_job_question(action: CreateForwardTestAction, schedule: dict[str, str], forward_test_id: str | None = None) -> str:
    schedule_prompt = schedule.get("prompt", "")
    role = schedule.get("role", "rebalance")
    forward_test_line = f"forward_test_id: {forward_test_id}\n" if forward_test_id else ""

    if action.strategy_type == "sql":
        base = (
            f"[포워드 테스트 리밸런싱] {action.name} - {role}\n"
            f"strategy_type: sql\n"
            f"{forward_test_line}"
            f"schedule_role: {role}\n"
            f"screening_sql: {action.screening_sql}\n"
            f"universe: {action.universe}\n"
        )
        default_instruction = (
            "screening_sql을 오늘 날짜로 실행하여 종목을 선정하고, "
            "현재가를 조회한 뒤 가용 현금 안에서 균등 배분으로 리밸런싱하세요. "
            "완료 후 execute_forward_trades에 주문 의도만 전달하세요. "
            "cash, holdings, avg_cost, total_value, return_pct, execution price는 직접 계산하지 마세요. "
            "단, 이 지시는 스케줄 실행 시점에 적용되며 생성 직후 즉시 실행하지 마세요."
        )
    else:
        base = (
            f"[포워드 테스트 실행] {action.name} - {role}\n"
            f"strategy_type: llm\n"
            f"{forward_test_line}"
            f"schedule_role: {role}\n"
            f"전략: {action.strategy_prompt}\n"
            f"universe: {action.universe}\n"
        )
        default_instruction = (
            "전략에 따라 현재 schedule_role에 맞는 판단을 수행하세요. "
            "완료 후 execute_forward_trades에 주문 의도만 전달하세요. "
            "cash, holdings, avg_cost, total_value, return_pct, execution price는 직접 계산하지 마세요. "
            "단, 이 지시는 스케줄 실행 시점에 적용되며 생성 직후 즉시 실행하지 마세요."
        )

    if schedule_prompt:
        return base + f"이번 실행 지시: {schedule_prompt}\n" + default_instruction
    return base + default_instruction


def _create_scheduled_job(
    user_id: str,
    state: Any,
    question: str,
    cron: str,
    *,
    after: datetime | None = None,
) -> tuple[str | None, str]:
    next_run_at = _parse_next_run_after(cron, after) if after else _parse_next_run(cron)
    job_result = _supabase_request(
        "scheduled_jobs",
        method="POST",
        body={
            "user_id": user_id,
            "question": question,
            "cron_expression": cron,
            "model_selection_id": state.get_agent_state("model_selection_id"),
            "enabled": True,
            "next_run_at": next_run_at,
        },
    )
    job_id = job_result[0]["id"] if isinstance(job_result, list) and job_result else None
    return job_id, next_run_at


def _link_forward_test_job(forward_test_id: str, job_id: str, role: str) -> None:
    _supabase_request(
        "forward_test_jobs",
        method="POST",
        body={
            "forward_test_id": forward_test_id,
            "job_id": job_id,
            "role": role,
        },
    )


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

    try:
        schedules = _build_schedules(action)
    except ValueError as exc:
        return CreateForwardTestObservation(
            success=False,
            message=str(exc),
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

    try:
        # 1. Create primary scheduled job first because forward_tests.job_id is required.
        primary_schedule = schedules[0]
        primary_question = _build_job_question(action, primary_schedule)
        job_id, next_run_at = _create_scheduled_job(
            user_id,
            state,
            primary_question,
            primary_schedule["cron_expression"],
        )
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
        if not ft_id:
            return CreateForwardTestObservation(
                success=False,
                message="포워드 테스트 생성에 실패했습니다.",
            )

        job_ids = [job_id]
        try:
            _link_forward_test_job(ft_id, job_id, primary_schedule["role"])
        except Exception as exc:
            if len(schedules) > 1:
                return CreateForwardTestObservation(
                    success=False,
                    message=f"포워드 테스트-스케줄 연결 생성 실패: {exc}",
                    forward_test_id=ft_id,
                    job_id=job_id,
                    job_ids=job_ids,
                )

        schedule_after = datetime.fromisoformat(next_run_at)
        for schedule in schedules[1:]:
            question = _build_job_question(action, schedule, ft_id)
            extra_job_id, extra_next_run_at = _create_scheduled_job(
                user_id,
                state,
                question,
                schedule["cron_expression"],
                after=schedule_after,
            )
            if not extra_job_id:
                return CreateForwardTestObservation(
                    success=False,
                    message=f"{schedule['role']} 스케줄 잡 생성에 실패했습니다.",
                    forward_test_id=ft_id,
                    job_id=job_id,
                    job_ids=job_ids,
                )
            _link_forward_test_job(ft_id, extra_job_id, schedule["role"])
            job_ids.append(extra_job_id)
            schedule_after = datetime.fromisoformat(extra_next_run_at)

        return CreateForwardTestObservation(
            success=True,
            message=f"다음 실행: {next_run_at[:16].replace('T', ' ')} UTC",
            forward_test_id=ft_id,
            job_id=job_id,
            job_ids=job_ids,
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
                    "description": "단일 실행 스케줄 cron (5필드). schedules가 있으면 생략 가능. 예: '0 0 * * 1-5' = 평일 09:00 KST",
                },
                "schedules": {
                    "type": "array",
                    "description": (
                        "하나의 포워드 테스트에 여러 실행 스케줄을 연결할 때 사용. "
                        "예: 11시 매수와 15시 매도를 같은 forward_test_id로 묶기."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "description": "스케줄 역할. 예: buy, sell, rebalance, monitor",
                            },
                            "cron_expression": {
                                "type": "string",
                                "description": "이 역할의 실행 cron (5필드)",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "이 실행 역할에서 수행할 구체 지시. 예: 11시에는 3종목 매수, 15시에는 전량 매도",
                            },
                        },
                        "required": ["role", "cron_expression"],
                    },
                },
                "initial_capital": {
                    "type": "number",
                    "description": "초기 자본금 (기본: 1억원)",
                },
            },
            "required": ["name", "strategy_type"],
        }


def make_create_forward_test_tool() -> CreateForwardTestTool:
    return CreateForwardTestTool(
        name="create_forward_test",
        description=(
            "포워드 테스트를 생성합니다. 전략을 정의하고 스케줄을 설정하면, "
            "에이전트가 자동으로 페이퍼 매매를 실행합니다. "
            "SQL 기반(백테스트 screening_sql 재사용) 또는 LLM 기반(자연어 전략)을 선택할 수 있습니다. "
            "하나의 요청에 여러 시각/역할이 있으면 schedules 배열로 여러 scheduled_job을 만들고 "
            "모두 같은 forward_test_id에 연결하세요. "
            "생성은 주문 실행이 아니므로 사용자가 즉시 1회 실행을 명시하지 않았다면 "
            "생성 직후 execute_forward_trades를 호출하지 마세요."
        ),
        action_type=CreateForwardTestAction,
        observation_type=CreateForwardTestObservation,
        executor=_execute,
    )
