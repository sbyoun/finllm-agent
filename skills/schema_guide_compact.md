# Schema Reference (compact)

## 규칙
- stocks가 앵커. stock_id는 stocks.id에서. 없는 컬럼 상상 금지. proxy 대체 금지.
- Oracle: `;` 금지, `LIMIT` 금지 → `FETCH FIRST`, `"date"` 쌍따옴표, `TO_DATE()`.
- 넓은 표현: 실적→매출/영업이익/순이익, 밸류→PER/PBR/EV_EBITDA, 수급→순매수.
- 성공한 SQL 반복 금지. 에러 시 수정 재시도.

## 테이블
stocks: id|ticker|name|country|market|instrument_type (KR: stock|etf 1088개. US: stock만 싱크/SP1500 고정. ETF 필터 시 `instrument_type='etf'`, 주식만은 `='stock'`)
us_stock_snapshots: symbol|name|exchange|instrument_type|security_type|group_code (**나스닥: exchange='NAS'**, NYS/AMS도 동일. US ETF 유니버스 5505 여기만 있음, 가격 미싱크)
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

## KR 주요 account (account_name은 영문코드 grs/lblt_rate 등 — 한글 LIKE 검색 금지, id 직접 사용)
플로: 6592 매출 | 6597 영업이익 | 6603 순이익 | 6594 매출총이익 | 6590 ebitda | 6591 ev_ebitda | 6595 감가상각비(**99.99 더미, SELECT 금지. 역산: 6590-6597**)
주당/수익성: 6580 eps | 6581 sps | 6582 bps | 6579 ROE | 6614 총자본순이익률 | 6616 매출액순이익률 | 6617 매출총이익률
성장: 6576 매출성장률 | 6577 영업이익증가율 | 6578 순이익증가율 | 6618 자기자본증가율 | 6619 총자산증가율
안정/배당: 6583 유보율 | 6584 부채비율 | 6586 유동비율 | 6587 당좌비율 | 6588 배당성향
재무상태: 6604 유동자산 | 6605 비유동자산 | 6606 총자산 | 6607 유동부채 | 6608 비유동부채 | 6609 총부채 | 6613 자기자본
**미제공**: DPS/배당금/시가배당률, ROCE 직접, 순현금 직접, 현금성자산 직접. ROCE는 ROE(6579) 근사, 순현금은 유동자산(6604)>총부채(6609) 판정. (한국 ETF는 `stocks.instrument_type='etf'`, 나스닥은 `us_stock_snapshots.exchange='NAS'` 이제 제공.)

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
