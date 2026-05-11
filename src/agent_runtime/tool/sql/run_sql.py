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
    method_summary: str | None = None
    assumptions: str | None = None
    caveats: str | None = None
    role: str = "final"  # "final" = analysis result for display, "diagnostic" = exploration/schema lookup


@dataclass(slots=True)
class RunSQLObservation(Observation):
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    role: str = "final"
    method_summary: str | None = None
    assumptions: str | None = None
    caveats: str | None = None
    preview_limit: int = 100

    def to_text(self) -> str:
        preview_rows = self.rows[: self.preview_limit]
        lines = [
            f"row_count={self.row_count}",
            f"columns={self.columns}",
        ]
        if self.method_summary:
            lines.append(f"method_summary={self.method_summary}")
        if self.assumptions:
            lines.append(f"assumptions={self.assumptions}")
        if self.caveats:
            lines.append(f"caveats={self.caveats}")
        lines.extend(
            [
                f"preview_row_count={len(preview_rows)}",
                f"preview_rows={preview_rows}",
            ]
        )
        return "\n".join(lines)


class RunSQLTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute"},
                "title": {"type": "string", "description": "Title for the result dataset"},
                "description": {"type": "string", "description": "Description of what this query does"},
                "method_summary": {
                    "type": "string",
                    "description": (
                        "Human-readable implementation summary for final/data queries. "
                        "State the universe, as-of/date basis, factor/filter definitions, ranking/sorting direction, "
                        "and any important joins/date lags actually implemented by this SQL. Do not paste SQL."
                    ),
                },
                "assumptions": {
                    "type": "string",
                    "description": (
                        "User-visible assumptions made by this SQL, especially defaults not explicitly specified by the user. "
                        "Use '없음' only when there are no material assumptions."
                    ),
                },
                "caveats": {
                    "type": "string",
                    "description": (
                        "User-visible caveats and excluded conditions, such as missing 거래정지/관리종목 filters, "
                        "hardcoded periods, approximate metrics, or data limitations. Use '없음' only when none are material."
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": ["final", "diagnostic"],
                    "description": "Default to 'final'. Use 'diagnostic' ONLY for schema/metadata lookups (e.g., listing tables, finding column names, resolving stock IDs). Any query that returns actual market data, financials, prices, or rankings MUST use 'final'.",
                },
            },
            "required": ["sql", "method_summary", "assumptions", "caveats"],
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
            method_summary=action.method_summary,
            assumptions=action.assumptions,
            caveats=action.caveats,
        )

    return RunSQLTool(
        name="run_sql",
        description=(
            "Execute SQL query. "
            "NEVER use SYSDATE/CURRENT_DATE/today's date — use MAX(\"date\") subquery instead. "
            "Oracle: never nest analytic/window functions (`... OVER (...)`, e.g. LAG/ROW_NUMBER) inside aggregates "
            "like STDDEV/AVG/SUM; compute window values in an inner CTE, aggregate in an outer CTE. "
            "For factor rankings with DESC, always use NULLS LAST or filter NULL factors before ranking. "
            "If summing PERCENT_RANK over DESC factors, sort the summed score ASC unless using 1-PERCENT_RANK. "
            "0 rows → retry with MAX(\"date\") or broader filters. "
            "role='final'(default) for data queries; role='diagnostic' ONLY for schema lookups. "
            "For every final/data query, fill method_summary, assumptions, and caveats so the final answer can show "
            "what was actually implemented. State actual date in answer (e.g. '4월 8일 기준'), not '오늘'."
        ),
        action_type=RunSQLAction,
        observation_type=RunSQLObservation,
        executor=_execute,
    )
