import unittest

from agent_runtime.tool.backtest.run_backtest import _normalize_screening_sql_date_anchors


class RunBacktestSqlNormalizationTest(unittest.TestCase):
    def test_normalizes_lookback_price_anchor_between_to_previous_trading_day(self) -> None:
        sql = """
            WITH dates AS (
                SELECT TO_DATE('{as_of_date}', 'YYYY-MM-DD') ref_dt,
                       ADD_MONTHS(TO_DATE('{as_of_date}', 'YYYY-MM-DD'), -12) m12_dt
                FROM dual
            ),
            p12 AS (
                SELECT stock_id, close
                FROM (
                    SELECT dp.stock_id, dp.close, dp."date",
                           ROW_NUMBER() OVER (PARTITION BY dp.stock_id ORDER BY dp."date" ASC) rn
                    FROM daily_prices dp
                    CROSS JOIN dates d
                    WHERE dp."date" BETWEEN d.m12_dt AND d.ref_dt
                )
                WHERE rn = 1
            )
            SELECT stock_id FROM p12
        """

        normalized, notes = _normalize_screening_sql_date_anchors(sql)

        self.assertNotIn("BETWEEN d.m12_dt AND d.ref_dt", normalized)
        self.assertIn('dp."date" = (SELECT MAX(fa_anchor_dp."date")', normalized)
        self.assertIn("fa_anchor_dp.stock_id = dp.stock_id", normalized)
        self.assertIn('fa_anchor_dp."date" <= d.m12_dt', normalized)
        self.assertEqual(len(notes), 1)

    def test_keeps_rolling_m3_window_unchanged(self) -> None:
        sql = """
            SELECT dp.stock_id, STDDEV(ret) volatility_3m
            FROM daily_prices dp
            CROSS JOIN dates d
            WHERE dp."date" BETWEEN d.m3_dt AND d.ref_dt
            GROUP BY dp.stock_id
        """

        normalized, notes = _normalize_screening_sql_date_anchors(sql)

        self.assertEqual(normalized, sql)
        self.assertEqual(notes, [])

    def test_keeps_unaliased_date_conditions_unchanged(self) -> None:
        sql = 'SELECT stock_id FROM daily_prices WHERE "date" BETWEEN m12_dt AND ref_dt'

        normalized, notes = _normalize_screening_sql_date_anchors(sql)

        self.assertEqual(normalized, sql)
        self.assertEqual(notes, [])

    def test_scopes_global_daily_price_max_date_to_kr_universe(self) -> None:
        sql = """
            WITH dates AS (
                SELECT
                    (SELECT MAX("date") FROM daily_prices WHERE "date" <= prev_month_end) AS d_ref,
                    (SELECT MAX("date") FROM daily_prices WHERE "date" <= ADD_MONTHS(prev_month_end, -6)) AS d_6m
                FROM params
            )
            SELECT stock_id FROM daily_prices
        """

        normalized, notes = _normalize_screening_sql_date_anchors(sql, universe="KOSPI")

        self.assertNotIn('FROM daily_prices WHERE "date" <= prev_month_end', normalized)
        self.assertIn("JOIN stocks fa_market_stock ON fa_market_stock.id = fa_market_dp.stock_id", normalized)
        self.assertIn("fa_market_stock.country = 'KR'", normalized)
        self.assertIn("fa_market_stock.market = 'KOSPI'", normalized)
        self.assertIn('fa_market_dp."date" <= prev_month_end', normalized)
        self.assertIn('fa_market_dp."date" <= ADD_MONTHS(prev_month_end, -6)', normalized)
        self.assertEqual(len(notes), 2)

    def test_keeps_stock_scoped_max_date_anchor_unchanged(self) -> None:
        sql = """
            SELECT (
                SELECT MAX(dp."date")
                FROM daily_prices dp
                WHERE dp.stock_id = s.id AND dp."date" <= m12_dt
            ) AS d_12m
            FROM stocks s
        """

        normalized, notes = _normalize_screening_sql_date_anchors(sql, universe="KOSPI")

        self.assertEqual(normalized, sql)
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
