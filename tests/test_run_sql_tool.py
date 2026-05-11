import unittest

from agent_runtime.tool.sql.run_sql import RunSQLAction, make_run_sql_tool


class RunSQLToolTest(unittest.TestCase):
    def test_oracle_window_pattern_is_not_hard_blocked(self) -> None:
        calls: list[str] = []

        def runner(sql: str):
            calls.append(sql)
            return ["ok"], [{"ok": 1}]

        tool = make_run_sql_tool(runner)
        observation = tool(
            RunSQLAction(
                sql=(
                    'SELECT stock_id, STDDEV(close / LAG(close) OVER (PARTITION BY stock_id ORDER BY "date") - 1) '
                    'FROM daily_prices GROUP BY stock_id'
                )
            )
        )

        self.assertEqual(observation.row_count, 1)
        self.assertEqual(len(calls), 1)

    def test_ranking_direction_pattern_is_not_hard_blocked(self) -> None:
        calls: list[str] = []

        def runner(sql: str):
            calls.append(sql)
            return ["ticker"], [{"ticker": "005930"}]

        tool = make_run_sql_tool(runner)
        observation = tool(
            RunSQLAction(
                sql=(
                    "WITH ranked AS ("
                    "SELECT ticker, "
                    "PERCENT_RANK() OVER (ORDER BY ret_12m DESC NULLS LAST) AS total_score "
                    "FROM factors"
                    ") SELECT ticker, total_score FROM ranked ORDER BY total_score DESC FETCH FIRST 3 ROWS ONLY"
                )
            )
        )

        self.assertEqual(observation.row_count, 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
