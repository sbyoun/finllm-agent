from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition

_SQL_LOGGER = logging.getLogger("agent_runtime.run_sql")

# kis_kr financial_statements.account_id=6595(감가상각비)는 파이프라인이 전 종목에
# 99.99 더미값을 채워 넣어 실제 값이 아님. agent가 실수로 SELECT해 "감가상각비 99.99억"
# 같은 환각을 내놓지 못하도록 런타임에서 하드 블록한다. 역산은 6590(EBITDA)-6597(영업이익).
_DUMMY_6595_RE = re.compile(r"account_id\s*(=|IN\s*\()\s*6595", re.IGNORECASE)


class DummyAccountBlockedError(RuntimeError):
    pass


class SQLRunner(Protocol):
    def __call__(self, sql: str) -> tuple[list[str], list[dict]]:
        ...


@dataclass(slots=True)
class RunSQLAction(Action):
    sql: str = ""
    title: str | None = None
    description: str | None = None
    role: str = "final"  # "final" = analysis result for display, "diagnostic" = exploration/schema lookup


@dataclass(slots=True)
class RunSQLObservation(Observation):
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    role: str = "final"
    preview_limit: int = 100

    def to_text(self) -> str:
        preview_rows = self.rows[: self.preview_limit]
        return "\n".join(
            [
                f"row_count={self.row_count}",
                f"columns={self.columns}",
                f"preview_row_count={len(preview_rows)}",
                f"preview_rows={preview_rows}",
            ]
        )


class RunSQLTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute"},
                "title": {"type": "string", "description": "Title for the result dataset"},
                "description": {"type": "string", "description": "Description of what this query does"},
                "role": {
                    "type": "string",
                    "enum": ["final", "diagnostic"],
                    "description": "Default to 'final'. Use 'diagnostic' ONLY for schema/metadata lookups (e.g., listing tables, finding column names, resolving stock IDs). Any query that returns actual market data, financials, prices, or rankings MUST use 'final'.",
                },
            },
            "required": ["sql"],
        }


def make_run_sql_tool(runner: SQLRunner) -> RunSQLTool:
    def _execute(action: RunSQLAction, conversation: object | None = None) -> RunSQLObservation:
        if _DUMMY_6595_RE.search(action.sql or ""):
            _SQL_LOGGER.warning("Blocked SQL referencing dummy account_id=6595: %s", (action.sql or "")[:500])
            raise DummyAccountBlockedError(
                "account_id=6595(감가상각비)는 kis_kr이 전 종목에 99.99 더미값만 채워두었습니다. "
                "실제 감가상각비가 필요하면 `EBITDA(6590) - 영업이익(6597)`로 역산하세요 "
                "(동일 stock_id/year/quarter 기준). 역산 결과는 근사치임을 사용자에게 명시하세요."
            )
        columns, rows = runner(action.sql)
        return RunSQLObservation(
            content=[],
            columns=columns,
            rows=rows,
            row_count=len(rows),
            role=action.role,
        )

    return RunSQLTool(
        name="run_sql",
        description=(
            "Execute SQL query. "
            "REGION DEFAULT: when the user did NOT specify a region, filter to Korea only (country='KR'). Do NOT query US/global without an explicit user request. "
            "NEVER use SYSDATE/CURRENT_DATE/today's date — use MAX(\"date\") subquery instead. "
            "0 rows → retry with MAX(\"date\") or broader filters. "
            "role='final'(default) for data queries; role='diagnostic' ONLY for schema lookups. "
            "State actual date in answer (e.g. '4월 8일 기준'), not '오늘'."
        ),
        action_type=RunSQLAction,
        observation_type=RunSQLObservation,
        executor=_execute,
    )
