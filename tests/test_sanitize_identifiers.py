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


class PlainMathTest(unittest.TestCase):
    def test_block_and_frac(self):
        self.assertEqual(
            clean(r"$$\text{사모펀드 매수강도(\%)} = \frac{\text{사모펀드 순매수대금(억원)}}{\text{시가총액(억원)}} \times 100$$"),
            "사모펀드 매수강도(%) = 사모펀드 순매수대금(억원) ÷ 시가총액(억원) × 100",
        )
        self.assertEqual(
            clean(r"$$\text{반등 목표치} = 137.9\text{만 원} + (20.8\text{만 원} \times 0.382) \approx \mathbf{145.8\text{만 원}}$$"),
            "반등 목표치 = 137.9만 원 + (20.8만 원 × 0.382) ≈ 145.8만 원",
        )

    def test_inline(self):
        self.assertEqual(clean(r"6,012pt $\rightarrow$ 5,937pt $\rightarrow$ 5,730pt"), "6,012pt → 5,937pt → 5,730pt")
        self.assertEqual(clean(r"기대 상승률: $+5.5\% \times 2.0 \approx \mathbf{+11.0\%}$"), "기대 상승률: +5.5% × 2.0 ≈ +11.0%")
        self.assertEqual(clean(r"약 **547,577원** ($400\text{조 원} \div 7.3049\text{억 주}$)"), "약 **547,577원** (400조 원 ÷ 7.3049억 주)")

    def test_dollar_amounts_untouched(self):
        for s in ["엔비디아는 $120에서 $135로 올랐습니다.", "매출 $35.1B, EPS $0.81", "환율 1,400원/$ 기준 $100 = 14만 원"]:
            self.assertEqual(clean(s), s)


if __name__ == "__main__":
    unittest.main()
