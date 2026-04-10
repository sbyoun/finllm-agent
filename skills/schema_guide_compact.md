# Schema Reference (compact)

## 규칙
- stocks가 앵커. stock_id는 stocks.id에서. 없는 컬럼 상상 금지. proxy 대체 금지.
- Oracle: `;` 금지, `LIMIT` 금지 → `FETCH FIRST`, `"date"` 쌍따옴표, `TO_DATE()`.
- 넓은 표현: 실적→매출/영업이익/순이익, 밸류→PER/PBR/EV_EBITDA, 수급→순매수.
- 성공한 SQL 반복 금지. 에러 시 수정 재시도.

## 테이블
stocks: id|ticker|name|country|market
daily_prices: stock_id|"date"|open|high|low|close|volume (NO per/pbr/market_cap — 등락률은 LAG)
benchmark_daily_prices: symbol|"date"|close (KS11/SPY/QQQ)
financial_statements: stock_id|account_id|year|quarter|value (long table, account_id는 목록에서만)
financial_accounts: id|account_name|account_type
kr_investor_trade_daily: stock_id|"date"|foreign/personal/institution_net_value|close (*_value=백만원)
kr_program_trade_daily: stock_id|"date"|program_net_value|close (*_value=백만원)
kr_market_investor_daily: market_code|"date"|foreign/personal/institution_net_value (*_value=백만원)
kr_market_program_daily: market_code|"date"|whole/arbitrage/nonarbitrage_net_value (*_value=백만원)
stock_sectors: stock_id|sector|sector_group
kr_loan_daily: stock_id|"date"|remaining_qty|remaining_amount(백만원)
kr_short_sale_daily: stock_id|"date"|short_sale_qty/value|short_sale_volume_ratio (*_value=백만원)
kr_stock_snapshots: ticker|"date"|market_cap(백만원)|listed_shares|kospi200_sector (join via ticker, from 2026-03-09)

## KR 주요 account
6592 매출 | 6597 영업이익 | 6603 순이익 | 6594 매출총이익 | 6590 ebitda | 6591 ev_ebitda | 6580 eps | 6581 sps | 6582 bps | 6579 roe_val

## 파생 지표
PBR=close/bps(6582) | PER=close/eps(6580) | OpMargin=6597/6592
PER/PBR 계산: financial_statements(account_id=6580 or 6582) JOIN daily_prices ON stock_id, latest quarter

## 데이터 커버리지 (백테스트 기간 설정 시 필수 확인)
daily_prices: 2009-12-31~ | financial_statements: 2004-Q1~ | benchmark_daily_prices: 2009-12-31~
kr_short_sale_daily: 2024-01-02~ | kr_loan_daily: 2024-01-02~
kr_market_investor_daily: 2024-12-16~ | kr_program_trade_daily: 2025-07-03~
kr_investor_trade_daily: 2026-01-23~ (≈2.5개월) | kr_stock_snapshots: 2026-03-09~ (스크리닝 전용)
수급 조건 백테스트는 데이터 시작일 이후 기간만 유효. 부족 시 가용 기간으로 조정하거나 사용자에게 안내.

섹터 목록, 전체 account list, canonical SQL 패턴 → 이전 컨텍스트 참조.
