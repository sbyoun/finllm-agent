from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent_runtime.agent.agent import Agent
from agent_runtime.conversation.conversation import create_conversation
from agent_runtime.conversation.local_conversation import LocalConversation
from agent_runtime.conversation.state import ConversationExecutionStatus
from agent_runtime.context import LLMSummarizingCondenser
from agent_runtime.env import load_env
from agent_runtime.event.action import ActionEvent
from agent_runtime.event.condensation import CondensationEvent
from agent_runtime.event.message import MessageEvent
from agent_runtime.event.observation import AgentErrorEvent, ObservationEvent
from agent_runtime.event.state_update import ConversationStateUpdateEvent
from agent_runtime.llm import RuntimeLlmConfig, create_default_llm_client, create_llm_client
from agent_runtime.prompt import DEFAULT_SKILL_FILES, DEFAULT_SKILL_FILES_COMPACT, build_system_prompt
from agent_runtime.tool.news import SearchNewsObservation, make_search_news_tool
from agent_runtime.tool.portfolio import make_get_portfolio_tool
from agent_runtime.tool.sql import RunSQLAction, RunSQLObservation, make_run_sql_tool
from agent_runtime.tool.sql.oracle import OracleSQLRunner
from agent_runtime.tool.jobs.register_job import make_register_job_tool
from agent_runtime.tool.backtest.run_backtest import RunBacktestObservation, make_run_backtest_tool
from agent_runtime.tool.forward_test import (
    make_create_forward_test_tool,
    make_save_forward_snapshot_tool,
    make_get_forward_test_tool,
)


EventCallback = Callable[[dict[str, Any]], None]


_SANITIZE_LOGGER = logging.getLogger("agent_runtime.sanitize")

_FENCED_CODE_RE = re.compile(r"```(?:sql|python|SQL|Python)\b.*?```", re.DOTALL)
_RAW_PRELUDE_RE = re.compile(
    r"^\s*(?:SELECT|WITH|INSERT|UPDATE|DELETE|import\s|from\s|def\s|async\s+def\s)",
    re.IGNORECASE,
)


def _sanitize_assistant_message(content: str) -> str:
    original = content
    removed: list[str] = []

    def _capture_fence(match: re.Match[str]) -> str:
        removed.append(match.group(0))
        return ""

    cleaned = _FENCED_CODE_RE.sub(_capture_fence, content)

    cleaned_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if (
            line.startswith("TOOL_CALL[")
            or line.startswith("TOOL_RESULT[")
            or line.startswith("[Called:")
            or line.startswith("[Result:")
        ):
            removed.append(raw_line)
            continue
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        cleaned_lines.append(raw_line)

    cleaned = "\n".join(cleaned_lines).strip()

    # Raw SQL/Python prelude: drop leading paragraph(s) until we hit prose.
    while cleaned and _RAW_PRELUDE_RE.match(cleaned):
        head, sep, tail = cleaned.partition("\n\n")
        removed.append(head)
        cleaned = tail.strip() if sep else ""

    if removed:
        _SANITIZE_LOGGER.warning(
            "Stripped leaked tool/script content from assistant message: original_len=%d cleaned_len=%d removed=%s",
            len(original),
            len(cleaned),
            json.dumps(removed, ensure_ascii=False)[:2000],
        )

    return cleaned


def _strip_markdown_tables(content: str) -> str:
    """Remove markdown tables from assistant message when data panel will show the table."""
    import re
    lines = content.split("\n")
    result: list[str] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            continue
        if in_table and (not stripped or stripped.startswith("|")):
            if stripped.startswith("|"):
                continue
            in_table = False
            continue
        in_table = False
        result.append(line)
    # Clean up multiple blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(result))
    return cleaned.strip()


@dataclass(slots=True)
class RuntimeMessageContext:
    role: str
    content: str


@dataclass(slots=True)
class RuntimeDisplaySpec:
    type: str
    title: str
    preferredColumns: list[str] | None = None


@dataclass(slots=True)
class RuntimeToolRequest:
    kind: str
    sql: str
    reason: str
    display: RuntimeDisplaySpec


@dataclass(slots=True)
class RuntimePlannerDecision:
    mode: str
    assistantMessage: str
    clarificationQuestion: str | None = None
    toolRequest: RuntimeToolRequest | None = None


@dataclass(slots=True)
class RuntimeDataColumn:
    key: str
    label: str


@dataclass(slots=True)
class RuntimeAnalysisDataset:
    title: str
    description: str
    columns: list[RuntimeDataColumn]
    rows: list[dict[str, str | int | float | None]]


@dataclass(slots=True)
class RuntimeExecutionMetrics:
    loopCount: int
    actionCount: int
    toolCallCount: int
    sqlCallCount: int
    newsCallCount: int
    skillLoadCount: int
    errorCount: int
    condensationCount: int
    assistantMessageCount: int


@dataclass(slots=True)
class RuntimeAgentRequest:
    question: str
    user_id: str | None = None
    session_id: str | None = None
    history: list[RuntimeMessageContext] = field(default_factory=list)
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    llm_config: RuntimeLlmConfig | None = None
    max_iterations: int = 8


@dataclass(slots=True)
class RuntimeAgentResult:
    runId: str
    decision: RuntimePlannerDecision
    dataset: RuntimeAnalysisDataset | None
    datasets: list[RuntimeAnalysisDataset]
    sql: str | None
    sqlScripts: list[str]
    metrics: RuntimeExecutionMetrics
    executionLog: list[str]
    events: list[dict[str, Any]]
    elapsedMs: int
    status: str
    stateSnapshot: dict[str, Any] = field(default_factory=dict)
    tracePath: str | None = None


def serialize_event(event: object) -> dict[str, Any]:
    base = {
        "event_type": getattr(event, "event_type", type(event).__name__),
        "source": getattr(event, "source", None),
        "timestamp": getattr(event, "timestamp", None),
    }
    if isinstance(event, MessageEvent):
        base.update(
            {
                "role": event.role,
                "content": event.content,
            }
        )
    elif isinstance(event, ActionEvent):
        base.update(
            {
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "thought": event.thought,
                "action": repr(event.action),
            }
        )
    elif isinstance(event, ObservationEvent):
        base.update(
            {
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "observation_text": event.observation.to_text(),
            }
        )
    elif isinstance(event, AgentErrorEvent):
        base.update(
            {
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "error": event.error,
            }
        )
    elif isinstance(event, ConversationStateUpdateEvent):
        base.update(
            {
                "key": event.key,
                "operation": event.operation,
                "value": event.value,
            }
        )
    elif isinstance(event, CondensationEvent):
        base.update(
            {
                "forgotten_event_ids": event.forgotten_event_ids,
                "summary": event.summary,
                "summary_offset": event.summary_offset,
            }
        )
    else:
        base["repr"] = repr(event)
    return base


def _build_tools(*, repo_root: Path):
    return [
        make_get_portfolio_tool(),
        make_search_news_tool(),
        make_run_sql_tool(OracleSQLRunner()),
        make_register_job_tool(),
        make_run_backtest_tool(),
        make_create_forward_test_tool(),
        make_save_forward_snapshot_tool(),
        make_get_forward_test_tool(),
    ]


def _build_agent(*, repo_root: Path, llm_config: RuntimeLlmConfig | None) -> Agent:
    tools = _build_tools(repo_root=repo_root)
    llm = create_llm_client(llm_config) if llm_config else create_default_llm_client()
    model_name = (llm_config.model if llm_config else "").lower()
    is_opus = "opus" in model_name
    if is_opus:
        # Opus는 prompt caching이 안정적으로 돌면서 30K/min 한도가 사실상 완화됨.
        # 너무 공격적으로 압축하면 tool result가 요약되어 agent가 같은 SQL을 3번씩
        # 재실행하는 부작용이 있음 (samsung-points 시나리오에서 관측). 여유를 두고
        # 자연스럽게 누적되도록 완화.
        condenser = LLMSummarizingCondenser(
            llm=llm,
            max_size=24,
            target_size=14,
            keep_first=2,
            max_chars=100_000,
        )
    else:
        condenser = LLMSummarizingCondenser(llm=llm, max_size=24, keep_first=2)
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt=build_system_prompt(tools=tools, repo_root=repo_root),
        repo_root=str(repo_root),
        skill_files=DEFAULT_SKILL_FILES,
        skill_files_compact=DEFAULT_SKILL_FILES_COMPACT,
        condenser=condenser,
    )


MAX_HISTORY_CHARS = 40_000  # ~10K tokens, well within context limits
RECENT_KEEP = 6  # keep last 6 messages (3 turns) intact after compaction


def _estimate_history_chars(history: list[RuntimeMessageContext]) -> int:
    return sum(len(item.content) for item in history)


def _compact_history(
    history: list[RuntimeMessageContext],
    llm: object,
) -> list[RuntimeMessageContext]:
    """Compact long history: summarize older messages, keep recent ones."""
    if _estimate_history_chars(history) <= MAX_HISTORY_CHARS:
        return history

    recent = history[-RECENT_KEEP:] if len(history) > RECENT_KEEP else history
    older = history[:-RECENT_KEEP] if len(history) > RECENT_KEEP else []

    if not older:
        return history

    # Build summary of older messages
    lines = []
    for item in older:
        role_label = "User" if item.role == "user" else "Assistant"
        content_preview = item.content[:500]
        lines.append(f"{role_label}: {content_preview}")

    summary_prompt = (
        "Summarize the following conversation history concisely. "
        "Preserve: user goals, key findings, data points mentioned, "
        "unresolved questions, and important context for follow-up.\n\n"
        + "\n\n".join(lines)
    )

    try:
        response = llm.completion(
            messages=[
                {"role": "system", "content": "You compress conversation history. Keep only durable facts and context."},
                {"role": "user", "content": summary_prompt},
            ],
            tools=None,
        )
        summary_text = response.message.content.strip()
    except Exception:
        # Fallback: just take last N lines
        summary_text = "\n".join(
            f"- {item.role}: {item.content[:200]}" for item in older[-6:]
        )

    summary_message = RuntimeMessageContext(
        role="assistant",
        content=f"[Previous conversation summary]\n{summary_text}",
    )

    return [summary_message] + recent


def _hydrate_history(
    conversation: LocalConversation,
    history: list[RuntimeMessageContext],
) -> None:
    for item in history:
        conversation.state.event_log.append(
            MessageEvent(
                source="history",
                role="assistant" if item.role == "assistant" else "user",
                content=item.content,
            )
        )


def _emit(callback: EventCallback | None, payload: dict[str, Any]) -> None:
    if callback:
        callback(payload)


def _status_event(message: str, *, step_index: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "status", "message": message}
    if step_index is not None:
        payload["payload"] = {"stepIndex": step_index}
    return payload


def _map_runtime_event(event: object) -> dict[str, Any] | None:
    if isinstance(event, ActionEvent):
        payload: dict[str, Any] = {
            "tool": event.tool_name,
            "toolCallId": event.tool_call_id,
        }
        if isinstance(event.action, RunSQLAction):
            payload["sql"] = event.action.sql
            if event.action.title:
                payload["title"] = event.action.title
        elif event.tool_name == "load_skill" and event.action is not None:
            skill_name = getattr(event.action, "skill_name", None)
            if isinstance(skill_name, str) and skill_name.strip():
                payload["skillName"] = skill_name
        return {
            "type": "tool",
            "message": f"Tool: {event.tool_name}",
            "payload": payload,
        }
    if isinstance(event, ObservationEvent):
        payload: dict[str, Any] = {
            "tool": event.tool_name,
            "toolCallId": event.tool_call_id,
        }
        observation_text = event.observation.to_text()
        payload["observationText"] = observation_text
        if isinstance(event.observation, RunSQLObservation):
            payload["rowCount"] = event.observation.row_count
            payload["columns"] = event.observation.columns
        else:
            rows = getattr(event.observation, "rows", None)
            if isinstance(rows, list):
                payload["rowCount"] = len(rows)
        return {
            "type": "observation",
            "message": f"Observation: {event.tool_name}",
            "payload": payload,
        }
    if isinstance(event, AgentErrorEvent):
        return {
            "type": "error",
            "message": event.error,
            "payload": {"tool": event.tool_name},
        }
    if isinstance(event, MessageEvent) and event.role == "assistant":
        return {
            "type": "final",
            "message": event.content or "최종 답변을 정리하고 있습니다.",
        }
    return None


def _build_dataset_from_sql(action: RunSQLAction, observation: RunSQLObservation) -> RuntimeAnalysisDataset:
    return RuntimeAnalysisDataset(
        title=action.title or "SQL Result",
        description=action.description or "",
        columns=[RuntimeDataColumn(key=column, label=column) for column in observation.columns],
        rows=observation.rows,
    )


def _build_result(
    conversation: LocalConversation,
    *,
    elapsed_ms: int,
    loop_count: int,
    run_events: list[object],
) -> RuntimeAgentResult:
    serialized_events = [serialize_event(event) for event in run_events]
    execution_log: list[str] = []
    final_message = ""
    last_sql_action: RunSQLAction | None = None
    last_sql_observation: RunSQLObservation | None = None
    final_sql_results: list[tuple[RunSQLAction, RunSQLObservation]] = []
    clarification_question: str | None = None
    saw_error = False
    action_count = 0
    sql_call_count = 0
    news_call_count = 0
    skill_load_count = 0
    error_count = 0
    condensation_count = 0
    assistant_message_count = 0

    pending_sql_action: RunSQLAction | None = None
    for event in run_events:
        if isinstance(event, ActionEvent):
            action_count += 1
            execution_log.append(f"tool:{event.tool_name}")
            if isinstance(event.action, RunSQLAction):
                pending_sql_action = event.action
            if event.tool_name == "run_sql":
                sql_call_count += 1
            elif event.tool_name == "search_news":
                news_call_count += 1
            elif event.tool_name == "load_skill":
                skill_load_count += 1
        elif isinstance(event, ObservationEvent):
            if isinstance(event.observation, RunSQLObservation):
                obs = event.observation
                is_lookup = set(obs.columns) <= {"id", "ticker", "name", "stock_id", "sector_group", "sector"}
                effective_role = "diagnostic" if is_lookup else obs.role
                if effective_role == "final":
                    last_sql_observation = obs
                    last_sql_action = pending_sql_action
                    if pending_sql_action is not None:
                        final_sql_results.append((pending_sql_action, obs))
            elif isinstance(event.observation, SearchNewsObservation):
                execution_log.append(f"news_rows:{len(event.observation.rows)}")
        elif isinstance(event, AgentErrorEvent):
            saw_error = True
            error_count += 1
            execution_log.append(f"error:{event.tool_name}:{event.error}")
        elif isinstance(event, CondensationEvent):
            condensation_count += 1
        elif isinstance(event, MessageEvent) and event.role == "assistant":
            assistant_message_count += 1
            final_message = _sanitize_assistant_message(event.content)

    dataset = None
    datasets: list[RuntimeAnalysisDataset] = []
    sql = None
    sql_scripts: list[str] = []
    tool_request = None
    mode = "answer-only"

    # Check for backtest observation
    last_backtest_obs: RunBacktestObservation | None = None
    for event in run_events:
        if isinstance(event, ObservationEvent) and isinstance(event.observation, RunBacktestObservation):
            if event.observation.success:
                last_backtest_obs = event.observation

    if last_backtest_obs:
        columns = [
            RuntimeDataColumn(key="period", label="기간"),
            RuntimeDataColumn(key="return_pct", label="수익률(%)"),
            RuntimeDataColumn(key="benchmark_pct", label="벤치마크(%)"),
            RuntimeDataColumn(key="excess_pct", label="초과수익(%)"),
            RuntimeDataColumn(key="holdings", label="종목수"),
            RuntimeDataColumn(key="portfolio_value", label="포트폴리오"),
            RuntimeDataColumn(key="benchmark_value", label="벤치마크"),
        ]
        dataset = RuntimeAnalysisDataset(
            title="백테스트 분기별 성과",
            description=last_backtest_obs.summary,
            columns=columns,
            rows=last_backtest_obs.rows,
        )
        mode = "tool-result"
        final_message = _strip_markdown_tables(final_message)
        datasets = [dataset]
        tool_request = RuntimeToolRequest(
            kind="backtest",
            sql="",
            reason="Backtest completed",
            display=RuntimeDisplaySpec(
                type="table",
                title="백테스트 분기별 성과",
                preferredColumns=["period", "return_pct", "benchmark_pct", "excess_pct", "holdings"],
            ),
        )
    else:
        successful_sql_results = [
            (action, observation)
            for action, observation in final_sql_results
            if observation.row_count > 0
        ]
        if successful_sql_results:
            datasets = [
                _build_dataset_from_sql(action, observation)
                for action, observation in successful_sql_results
            ]
            sql_scripts = [action.sql for action, _ in successful_sql_results]
            last_successful_sql_action, last_successful_sql_observation = successful_sql_results[-1]
            dataset = _build_dataset_from_sql(last_successful_sql_action, last_successful_sql_observation)
            sql = last_successful_sql_action.sql
            mode = "tool-result"
            final_message = _strip_markdown_tables(final_message)
            tool_request = RuntimeToolRequest(
                kind="sql",
                sql=last_successful_sql_action.sql,
                reason="Agent completed after tool loop",
                display=RuntimeDisplaySpec(
                    type="table",
                    title=dataset.title,
                    preferredColumns=[column.key for column in dataset.columns[:8]],
                ),
            )

    if conversation.state.execution_status == ConversationExecutionStatus.ERROR and not final_message:
        mode = "clarification"
        clarification_question = "질문을 처리하는 중 오류가 발생했습니다. 조건이나 대상을 조금 더 구체적으로 알려 주세요."
        final_message = "질문을 처리하는 중 오류가 발생했습니다."
    elif not final_message and saw_error and not dataset:
        mode = "clarification"
        clarification_question = "질문을 처리하는 중 오류가 발생했습니다. 조건이나 대상을 조금 더 구체적으로 알려 주세요."
        final_message = "질문을 처리하는 중 오류가 발생했습니다."

    # 환각 fallback 가드 — datasets가 비어 있고 SQL을 2회 이상 시도한 세션은,
    # status가 finished(환각 위험)든 error(일반 에러 메시지로 떨어짐)든 관계없이
    # 사과 문구로 override. news_call_count는 체크하지 않음 — news>0이어도 datasets가
    # 비어 있으면 결국 수치 근거 없이 답변하는 것이고, 정상 뉴스 답변(팔란티어)은
    # 가격 SQL로 datasets>=1이 잡혀 여기 걸리지 않음.
    if mode != "tool-result" and not datasets and sql_call_count >= 2:
        mode = "clarification"
        clarification_question = "조건이나 대상을 조금 더 구체적으로 알려 주시면 다시 시도해볼게요."
        final_message = (
            "죄송합니다. 요청하신 조건에 맞는 데이터를 정확히 조회하지 못했어요. "
            "탐색 과정에서 필요한 계정·지표·유니버스에 닿지 못해 신뢰할 수 있는 답변을 드릴 수 없는 상황입니다. "
            "조건이나 대상을 조금 더 구체적으로 알려 주시면 다시 시도해볼게요."
        )
        execution_log.append("fallback:no-final-sql-hallucination-guard")

    if not final_message and dataset:
        final_message = "데이터 조회 결과를 정리했습니다. 우측 데이터 패널에서 확인해 주세요."
    elif not final_message:
        mode = "clarification"
        clarification_question = "답변을 완성하지 못했습니다. 같은 질문을 다시 시도하거나 조건을 조금 더 구체적으로 알려 주세요."
        final_message = "답변을 완성하지 못했습니다. 다시 시도해 주세요."
        execution_log.append("fallback:empty-final-message")

    decision = RuntimePlannerDecision(
        mode=mode,
        assistantMessage=final_message,
        clarificationQuestion=clarification_question,
        toolRequest=tool_request,
    )
    metrics = RuntimeExecutionMetrics(
        loopCount=loop_count,
        actionCount=action_count,
        toolCallCount=action_count,
        sqlCallCount=sql_call_count,
        newsCallCount=news_call_count,
        skillLoadCount=skill_load_count,
        errorCount=error_count,
        condensationCount=condensation_count,
        assistantMessageCount=assistant_message_count,
    )

    return RuntimeAgentResult(
        runId=str(uuid4()),
        decision=decision,
        dataset=dataset,
        datasets=datasets,
        sql=sql,
        sqlScripts=sql_scripts,
        metrics=metrics,
        executionLog=execution_log,
        events=serialized_events,
        elapsedMs=elapsed_ms,
        status=str(conversation.state.execution_status),
        stateSnapshot=dict(conversation.state.agent_state),
    )


def _provider_name(config: RuntimeLlmConfig | None) -> str:
    if config is None:
        return "default"
    model = config.model.strip().lower()
    base_url = (config.base_url or "").strip().lower()
    if "gemini" in model or "generativelanguage.googleapis.com" in base_url:
        return "gemini"
    if "claude" in model or "anthropic" in base_url:
        return "anthropic"
    return "openai_compatible"


def _write_trace(
    *,
    repo_root: Path,
    run_id: str,
    request: RuntimeAgentRequest,
    result: RuntimeAgentResult,
    conversation: LocalConversation,
    streamed_events: list[dict[str, Any]],
) -> str:
    trace_dir = repo_root / "logs" / "agent_trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"run-{run_id}.json"
    payload = {
        "logged_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "request": {
            "question": request.question,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "history": [asdict(item) for item in request.history],
            "state_snapshot": request.state_snapshot,
            "max_iterations": request.max_iterations,
            "llm_config": {
                "provider": _provider_name(request.llm_config),
                "model": request.llm_config.model if request.llm_config else None,
                "base_url": request.llm_config.base_url if request.llm_config else None,
            },
        },
        "run_scope": {
            "history_event_count": len(conversation.state.event_log) - len(result.events),
            "run_event_count": len(result.events),
        },
        "result": {
            "runId": result.runId,
            "decision": asdict(result.decision),
            "dataset": asdict(result.dataset) if result.dataset else None,
            "sql": result.sql,
            "metrics": asdict(result.metrics),
            "executionLog": result.executionLog,
            "elapsedMs": result.elapsedMs,
            "status": result.status,
            "stateSnapshot": result.stateSnapshot,
        },
        "agent_state": conversation.state.agent_state,
        "streamed_events": streamed_events,
        "events": result.events,
    }
    trace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(trace_path)


def run_agent_request(
    request: RuntimeAgentRequest,
    *,
    on_event: EventCallback | None = None,
    repo_root: str | Path = str(Path(__file__).resolve().parent.parent.parent),
) -> RuntimeAgentResult:
    """Single runtime entrypoint used by CLI scripts and FastAPI wrappers."""
    load_env(Path(repo_root) / ".env")
    root = Path(repo_root)
    run_id = str(uuid4())
    agent = _build_agent(repo_root=root, llm_config=request.llm_config)
    conversation = create_conversation(agent, max_iterations=request.max_iterations)
    if request.state_snapshot:
        conversation.state.agent_state.update(request.state_snapshot)
    if request.user_id:
        conversation.state.set_agent_state("user_id", request.user_id)
    if request.session_id:
        conversation.state.set_agent_state("session_id", request.session_id)
    compacted_history = _compact_history(request.history, agent.llm)
    _hydrate_history(conversation, compacted_history)
    conversation.send_message(request.question)
    run_start_index = len(conversation.state.event_log) - 1

    started = time.perf_counter()
    streamed_events: list[dict[str, Any]] = []
    _emit(on_event, _status_event("분석을 시작합니다."))
    streamed_events.append(_status_event("분석을 시작합니다."))
    seen_events = run_start_index

    if conversation.state.execution_status in (
        ConversationExecutionStatus.IDLE,
        ConversationExecutionStatus.PAUSED,
        ConversationExecutionStatus.ERROR,
    ):
        conversation.state.execution_status = ConversationExecutionStatus.RUNNING

    iteration = 0
    while conversation.state.execution_status == ConversationExecutionStatus.RUNNING:
        _emit(
            on_event,
            _status_event(
                "질문을 이해하고 있습니다." if iteration == 0 else "다음 분석 단계를 결정하고 있습니다.",
                step_index=iteration + 1,
            ),
        )
        streamed_events.append(
            _status_event(
                "질문을 이해하고 있습니다." if iteration == 0 else "다음 분석 단계를 결정하고 있습니다.",
                step_index=iteration + 1,
            )
        )
        conversation.agent.step(conversation)

        events = list(conversation.state.event_log)
        for event in events[seen_events:]:
            mapped = _map_runtime_event(event)
            if mapped:
                _emit(on_event, mapped)
                streamed_events.append(mapped)
        seen_events = len(events)

        iteration += 1
        if iteration >= conversation.state.max_iterations:
            conversation.state.execution_status = ConversationExecutionStatus.ERROR
            _emit(
                on_event,
                {
                    "type": "error",
                    "message": "max_iterations exceeded",
                },
            )
            streamed_events.append(
                {
                    "type": "error",
                    "message": "max_iterations exceeded",
                }
            )
            break

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = _build_result(
        conversation,
        elapsed_ms=elapsed_ms,
        loop_count=iteration,
        run_events=list(conversation.state.event_log)[run_start_index:],
    )
    result.runId = run_id
    result.tracePath = _write_trace(
        repo_root=root,
        run_id=run_id,
        request=request,
        result=result,
        conversation=conversation,
        streamed_events=streamed_events,
    )
    return result


def run_agent_request_json(
    request: RuntimeAgentRequest,
    *,
    on_event: EventCallback | None = None,
    repo_root: str | Path = str(Path(__file__).resolve().parent.parent.parent),
) -> dict[str, Any]:
    result = run_agent_request(request, on_event=on_event, repo_root=repo_root)
    return asdict(result)
