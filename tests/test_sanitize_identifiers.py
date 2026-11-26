import sys
import types
import unittest

sys.modules.setdefault("oracledb", types.SimpleNamespace(connect=lambda **_: None))

from agent_runtime.service import _sanitize_assistant_message as clean


class HideInternalIdentifiersTest(unittest.TestCase):
    def test_removes_parenthesized_identifiers(self):
        src = "투자자별 거래 실적 테이블(`kr_investor_trade_daily`)의 **사모펀드 순매수대금(`private_equity_fund_net_value`)**을 당일 **시가총액(`market_cap`)**으로 나눕니다."
        self.assertEqual(clean(src), "투자자별 거래 실적 테이블의 **사모펀드 순매수대금**을 당일 **시가총액**으로 나눕니다.")

    def test_removes_lists_and_account_ids(self):
        self.assertEqual(clean("기준일: 2026-11-11 (`kr_stock_snapshots`, `daily_prices`)"), "기준일: 2026-11-11")
        self.assertEqual(clean("BPS는 2분기 기준 (`financial_statements`, `account_id=6582`)입니다."), "BPS는 2분기 기준입니다.")

    def test_keeps_normal_text(self):
        for s in ["PER(주가수익비율)은 12배입니다.", "종가는 `169,700원`입니다.", "삼성전자(005930) 수급"]:
            self.assertEqual(clean(s), s)


if __name__ == "__main__":
    unittest.main()
