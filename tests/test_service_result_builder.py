import sys
import types
import unittest

sys.modules.setdefault("oracledb", types.SimpleNamespace(connect=lambda **_: None))

from agent_runtime.agent.agent import Agent
from agent_runtime.conversation.local_conversation import LocalConversation
from agent_runtime.conversation.state import ConversationExecutionStatus, ConversationState
from agent_runtime.event.action import ActionEvent
from agent_runtime.event.message import MessageEvent
from agent_runtime.event.observation import ObservationEvent
from agent_runtime.service import _build_result
from agent_runtime.tool.backtest.run_backtest import RunBacktestAction, RunBacktestObservation
from agent_runtime.tool.sql import RunSQLAction, RunSQLObservation


def _sql_events(
    *,
    call_id: str,
    sql: str,
    title: str,
    rows: list[dict],
) -> list[object]:
    action = RunSQLAction(sql=sql, title=title)
    observation = RunSQLObservation(
        columns=list(rows[0].keys()) if rows else ["name", "ticker"],
        rows=rows,
        row_count=len(rows),
    )
    return [
        ActionEvent(tool_name="run_sql", tool_call_id=call_id, thought="", action=action),
        ObservationEvent(
            tool_name="run_sql",
            tool_call_id=call_id,
            action_id=call_id,
            observation=observation,
        ),
    ]


class ServiceResultBuilderTest(unittest.TestCase):
    def test_build_result_uses_previous_successful_sql_when_last_sql_is_empty(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        run_events: list[object] = [
            *_sql_events(
                call_id="positive",
                sql="select name, ticker, current_price from stocks where price <= 10000",
                title="1만원 이하 금융/통신 후보",
                rows=[{"name": "유진투자증권", "ticker": "001200", "current_price": 5010}],
            ),
            *_sql_events(
                call_id="empty",
                sql="select name, ticker from stocks where name like '%우'",
                title="1만원 이하 금융/통신 우선주",
                rows=[],
            ),
            MessageEvent(
                source="agent",
                role="assistant",
                content="조건을 모두 충족하는 종목은 없고, 앞선 후보를 대안으로 제시합니다.",
            ),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=2, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertEqual(result.dataset.title, "1만원 이하 금융/통신 후보")
        self.assertEqual(len(result.datasets), 1)
        self.assertEqual(
            result.datasets[0].rows,
            [{"name": "유진투자증권", "ticker": "001200", "current_price": 5010}],
        )
        self.assertEqual(result.sql, "select name, ticker, current_price from stocks where price <= 10000")
        self.assertEqual(result.sqlScripts, ["select name, ticker, current_price from stocks where price <= 10000"])
        self.assertNotIn("fallback:no-final-sql-hallucination-guard", result.executionLog)
        self.assertIn("### 실제 실행 기준", result.decision.assistantMessage)
        self.assertIn("실행 기준 메타데이터가 누락", result.decision.assistantMessage)

    def test_last_successful_sql_is_not_overwritten_by_empty_sql(self) -> None:
        agent = Agent(llm=None)  # type: ignore[arg-type]
        conversation = LocalConversation(
            agent=agent,
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )

        positive_action = RunSQLAction(
            sql="select name, ticker, current_price from stocks where price <= 10000",
            title="positive",
        )
        positive_observation = RunSQLObservation(
            columns=["name", "ticker", "current_price"],
            rows=[{"name": "유진투자증권", "ticker": "001200", "current_price": 5010}],
            row_count=1,
        )
        empty_action = RunSQLAction(sql="select name, ticker from stocks where name like '%우'", title="empty")
        empty_observation = RunSQLObservation(columns=["name", "ticker"], rows=[], row_count=0)

        agent._remember_observation(
            conversation,
            tool_name="run_sql",
            action=positive_action,
            observation=positive_observation,
        )
        agent._remember_observation(
            conversation,
            tool_name="run_sql",
            action=empty_action,
            observation=empty_observation,
        )

        last_successful_sql = conversation.state.agent_state["last_successful_sql"]
        self.assertEqual(last_successful_sql["title"], "positive")
        self.assertEqual(last_successful_sql["row_count"], 1)
        self.assertEqual(last_successful_sql["rows"], positive_observation.rows)
        self.assertEqual(last_successful_sql["rows_preview"], positive_observation.rows)
        self.assertEqual(conversation.state.agent_state["last_tool_result"]["title"], "empty")

    def test_state_context_includes_full_last_successful_sql_and_all_rows(self) -> None:
        agent = Agent(llm=None)  # type: ignore[arg-type]
        conversation = LocalConversation(
            agent=agent,
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        long_sql = (
            "WITH base AS (SELECT ticker, name FROM stocks WHERE market = 'KOSPI') "
            "SELECT ticker, name FROM base WHERE ticker IN ('005930', '000660', '035420') "
            "ORDER BY ticker /* keep-this-tail-marker */"
        )
        rows = [
            {"ticker": "000660", "name": "SK하이닉스"},
            {"ticker": "005930", "name": "삼성전자"},
            {"ticker": "035420", "name": "NAVER"},
        ]
        action = RunSQLAction(
            sql=long_sql,
            title="full-context-check",
            method_summary="KOSPI 종목 3개를 티커 기준으로 조회했습니다.",
            assumptions="없음",
            caveats="없음",
        )
        observation = RunSQLObservation(
            columns=["ticker", "name"],
            rows=rows,
            row_count=len(rows),
        )

        agent._remember_observation(
            conversation,
            tool_name="run_sql",
            action=action,
            observation=observation,
        )

        context = agent._state_context(conversation)

        self.assertIn("### Full Last Successful SQL Result", context)
        self.assertIn("keep-this-tail-marker", context)
        self.assertIn("KOSPI 종목 3개를 티커 기준으로 조회했습니다.", context)
        self.assertIn('"ticker": "000660"', context)
        self.assertIn('"ticker": "005930"', context)
        self.assertIn('"ticker": "035420"', context)

    def test_sql_result_includes_execution_notes(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        action = RunSQLAction(
            sql=(
                "select ticker, return_12m, stddev(ret) * 100 as volatility_3m "
                "from factors where volatility_3m <= 0.85"
            ),
            title="전략 후보",
            method_summary="KOSPI 후보를 12개월 수익률과 3개월 변동성 필터로 조회했습니다.",
            assumptions="재무 조건은 적용하지 않았습니다.",
            caveats="거래정지 제외 조건은 포함하지 않았습니다.",
        )
        observation = RunSQLObservation(
            columns=["ticker", "return_12m", "volatility_3m"],
            rows=[{"ticker": "005930", "return_12m": 0.1, "volatility_3m": 0.2}],
            row_count=1,
        )
        run_events: list[object] = [
            ActionEvent(tool_name="run_sql", tool_call_id="sql", thought="", action=action),
            ObservationEvent(tool_name="run_sql", tool_call_id="sql", action_id="sql", observation=observation),
            MessageEvent(source="agent", role="assistant", content="데이터 조회 결과입니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=1, run_events=run_events)

        self.assertIn("### 실제 실행 기준", result.decision.assistantMessage)
        self.assertIn("KOSPI 후보를 12개월 수익률과 3개월 변동성 필터로 조회했습니다.", result.decision.assistantMessage)
        self.assertIn("거래정지 제외 조건은 포함하지 않았습니다.", result.decision.assistantMessage)
        self.assertNotIn("### 자동 점검 경고", result.decision.assistantMessage)
        self.assertIsNotNone(result.dataset)
        self.assertIn("### 실제 실행 기준", result.dataset.description)

    def test_empty_sql_result_still_includes_execution_notes(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        action = RunSQLAction(
            sql="select name, ticker from stocks where 1 = 0",
            title="조건 충족 종목",
            method_summary="KOSPI 종목 중 최근 분기 적자전환 조건을 조회했습니다.",
            assumptions="최근 분기는 데이터베이스에 적재된 최대 분기로 해석했습니다.",
            caveats="조건을 충족하는 행이 0건이면 결과 테이블은 비어 있습니다.",
        )
        observation = RunSQLObservation(
            columns=["name", "ticker"],
            rows=[],
            row_count=0,
        )
        run_events: list[object] = [
            ActionEvent(tool_name="run_sql", tool_call_id="sql", thought="", action=action),
            ObservationEvent(tool_name="run_sql", tool_call_id="sql", action_id="sql", observation=observation),
            MessageEvent(source="agent", role="assistant", content="조건을 충족하는 종목은 없습니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=1, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertEqual(result.dataset.rows, [])
        self.assertIn("### 실제 실행 기준", result.decision.assistantMessage)
        self.assertIn("최근 분기 적자전환 조건", result.decision.assistantMessage)

    def test_ticker_name_only_final_sql_is_not_demoted_to_diagnostic(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        action = RunSQLAction(
            sql="select name, ticker from stocks fetch first 1 rows only",
            title="종목 후보",
            method_summary="종목명과 티커만 반환하는 최종 스크리닝 조회를 실행했습니다.",
            assumptions="없음",
            caveats="가격 및 재무 지표 컬럼은 결과에 포함하지 않았습니다.",
        )
        observation = RunSQLObservation(
            columns=["name", "ticker"],
            rows=[{"name": "삼성전자", "ticker": "005930"}],
            row_count=1,
        )
        run_events: list[object] = [
            ActionEvent(tool_name="run_sql", tool_call_id="sql", thought="", action=action),
            ObservationEvent(tool_name="run_sql", tool_call_id="sql", action_id="sql", observation=observation),
            MessageEvent(source="agent", role="assistant", content="조회 결과입니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=1, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertEqual(result.dataset.rows, [{"name": "삼성전자", "ticker": "005930"}])
        self.assertIn("종목명과 티커만 반환", result.decision.assistantMessage)

    def test_backtest_dataset_includes_execution_notes(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        action = RunBacktestAction(
            strategy_name="테스트 전략",
            screening_sql="select stock_id from stocks where {as_of_date} is not null",
            method_summary="KOSPI 월간 리밸런싱 백테스트를 실행했습니다.",
            assumptions="거래비용은 백테스트 기본값을 사용했습니다.",
            caveats="스크리닝 SQL의 세부 조건은 단순화되어 있습니다.",
        )
        observation = RunBacktestObservation(
            success=True,
            summary="테스트 전략: 1년간 KOSPI 대상.",
            rows=[{"period": "2026-01", "return_pct": 1.0, "benchmark_pct": 0.5, "excess_pct": 0.5, "holdings": 1}],
            row_count=1,
        )
        run_events: list[object] = [
            ActionEvent(tool_name="run_backtest", tool_call_id="bt", thought="", action=action),
            ObservationEvent(tool_name="run_backtest", tool_call_id="bt", action_id="bt", observation=observation),
            MessageEvent(source="agent", role="assistant", content="백테스트 결과입니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=1, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertIn("### 실제 실행 기준", result.decision.assistantMessage)
        self.assertIn("### 실제 실행 기준", result.dataset.description)

    def test_implicit_backtest_does_not_override_screening_sql_result(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        sql_action = RunSQLAction(
            sql="select name, ticker from ranked fetch first 3 rows only",
            title="월간 리밸런싱 후보",
            method_summary="KOSPI 월간 리밸런싱 후보 3개를 조회했습니다.",
            assumptions="없음",
            caveats="없음",
        )
        sql_observation = RunSQLObservation(
            columns=["name", "ticker"],
            rows=[{"name": "에이피알", "ticker": "278470"}],
            row_count=1,
        )
        backtest_action = RunBacktestAction(
            strategy_name="의도치 않은 백테스트",
            screening_sql="select stock_id from ranked where {as_of_date} is not null",
            method_summary="백테스트를 실행했습니다.",
            assumptions="없음",
            caveats="없음",
        )
        backtest_observation = RunBacktestObservation(
            success=True,
            summary="의도치 않은 백테스트: 5년간 KOSPI 대상.",
            rows=[],
            row_count=0,
        )
        run_events: list[object] = [
            MessageEvent(source="user", role="user", content="volatility_3m 정의만 이렇게 고쳐서 다시 보여줘."),
            ActionEvent(tool_name="run_sql", tool_call_id="sql", thought="", action=sql_action),
            ObservationEvent(tool_name="run_sql", tool_call_id="sql", action_id="sql", observation=sql_observation),
            ActionEvent(tool_name="run_backtest", tool_call_id="bt", thought="", action=backtest_action),
            ObservationEvent(tool_name="run_backtest", tool_call_id="bt", action_id="bt", observation=backtest_observation),
            MessageEvent(source="agent", role="assistant", content="후보 조회 결과입니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=2, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertEqual(result.dataset.title, "월간 리밸런싱 후보")
        self.assertEqual(result.dataset.rows, [{"name": "에이피알", "ticker": "278470"}])
        self.assertIsNotNone(result.decision.toolRequest)
        self.assertEqual(result.decision.toolRequest.kind, "sql")
        self.assertIn("result-builder:prefer-sql-over-implicit-backtest", result.executionLog)

    def test_explicit_backtest_keeps_backtest_dataset(self) -> None:
        conversation = LocalConversation(
            agent=None,  # type: ignore[arg-type]
            state=ConversationState(execution_status=ConversationExecutionStatus.FINISHED),
        )
        sql_action = RunSQLAction(
            sql="select name, ticker from ranked fetch first 3 rows only",
            title="월간 리밸런싱 후보",
            method_summary="KOSPI 월간 리밸런싱 후보 3개를 조회했습니다.",
            assumptions="없음",
            caveats="없음",
        )
        sql_observation = RunSQLObservation(
            columns=["name", "ticker"],
            rows=[{"name": "에이피알", "ticker": "278470"}],
            row_count=1,
        )
        backtest_action = RunBacktestAction(
            strategy_name="전략 백테스트",
            screening_sql="select stock_id from ranked where {as_of_date} is not null",
            method_summary="KOSPI 월간 리밸런싱 백테스트를 실행했습니다.",
            assumptions="없음",
            caveats="없음",
        )
        backtest_observation = RunBacktestObservation(
            success=True,
            summary="전략 백테스트: 5년간 KOSPI 대상.",
            rows=[{"period": "2026-01", "return_pct": 1.0, "benchmark_pct": 0.5, "holdings": 3}],
            row_count=1,
        )
        run_events: list[object] = [
            MessageEvent(source="user", role="user", content="이 전략을 백테스트해줘."),
            ActionEvent(tool_name="run_sql", tool_call_id="sql", thought="", action=sql_action),
            ObservationEvent(tool_name="run_sql", tool_call_id="sql", action_id="sql", observation=sql_observation),
            ActionEvent(tool_name="run_backtest", tool_call_id="bt", thought="", action=backtest_action),
            ObservationEvent(tool_name="run_backtest", tool_call_id="bt", action_id="bt", observation=backtest_observation),
            MessageEvent(source="agent", role="assistant", content="백테스트 결과입니다."),
        ]

        result = _build_result(conversation, elapsed_ms=1, loop_count=2, run_events=run_events)

        self.assertEqual(result.decision.mode, "tool-result")
        self.assertIsNotNone(result.dataset)
        self.assertEqual(result.dataset.title, "백테스트 분기별 성과")
        self.assertIsNotNone(result.decision.toolRequest)
        self.assertEqual(result.decision.toolRequest.kind, "backtest")
        self.assertNotIn("result-builder:prefer-sql-over-implicit-backtest", result.executionLog)


if __name__ == "__main__":
    unittest.main()
