from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


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
            "NEVER use SYSDATE/CURRENT_DATE/today's date — use MAX(\"date\") subquery instead. "
            "0 rows → retry with MAX(\"date\") or broader filters. "
            "role='final'(default) for data queries; role='diagnostic' ONLY for schema lookups. "
            "State actual date in answer (e.g. '4월 8일 기준'), not '오늘'."
        ),
        action_type=RunSQLAction,
        observation_type=RunSQLObservation,
        executor=_execute,
    )
