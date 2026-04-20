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
        self.assertEqual(last_successful_sql["rows_preview"], positive_observation.rows)
        self.assertEqual(conversation.state.agent_state["last_tool_result"]["title"], "empty")


if __name__ == "__main__":
    unittest.main()
