# Schema Guide

DB 질문이면 이 문서를 기준으로 SQL을 작성한다.

## 규칙

- `stocks`를 종목 식별의 앵커 테이블로 사용. `stock_id`는 `stocks.id`를 통해 얻는다.
- 문서에 없는 컬럼은 존재한다고 추정하지 않는다.
- 핵심 metric이 스키마에 없으면 SQL보다 `search_news` 또는 unavailable 안내가 우선.
- proxy metric 대체 금지: `market_cap ≠ close*volume`, `market share ≠ 매출순위`.
- 넓은 표현은 다의적으로 본다: 실적→매출/영업이익/순이익, 밸류→PER/PBR/EV_EBITDA, 수급→순매수.
- 구현 불가능한 조건은 조용히 약화하지 말고 explicitly unavailable.
- SQL 에러 → 같은 목표로 SQL 수정 재시도. 성공한 동일 SQL 반복 실행 금지.
- `낮은/높은/상위/하위` → SQL의 `where` 또는 `order by`에 반영.
- Oracle: `;` 금지, `LIMIT` 금지 → `FETCH FIRST N ROWS ONLY`, `"date"` 쌍따옴표 필수, 날짜는 `TO_DATE('YYYY-MM-DD','YYYY-MM-DD')`.

## 데이터 커버리지

| 테이블 | 시작 | 백테스트 |
|---|---|---|
| daily_prices | 2009-12-31 | ✅ 장기 |
| financial_statements | 2004-Q1 | ✅ 장기 |
| benchmark_daily_prices | 2009-12-31 | ✅ |
| kr_short_sale_daily | 2024-01-02 | ⚠️ ~2년 |
| kr_loan_daily | 2024-01-02 | ⚠️ ~2년 |
| kr_market_investor_daily | 2024-12-16 | ⚠️ ~1년 |
| kr_program_trade_daily | 2025-07-03 | ⚠️ ~9개월 |
| kr_market_program_daily | 2025-06-30 | ⚠️ ~9개월 |
| kr_investor_trade_daily | 2026-01-23 | ❌ ~2개월 |
| kr_stock_snapshots | 2026-03-09 | ❌ 스크리닝 전용 |

백테스트 시 사용 테이블의 시작일 확인 필수. 기간 부족 시 사용자에게 안내.

## 테이블 스키마

### stocks
cols: id | ticker | name | country | market
용도: 종목 식별 앵커. 가격/재무 조인 전 반드시 stocks에서 대상 확인.

### daily_prices
cols: stock_id | "date" | open | high | low | close | volume
join: stock_id → stocks.id
**없는 컬럼**: price_change_pct, pbr, per, market_cap, roe, dividend_yield, trading_date
등락률 → LAG 패턴으로 직접 계산. 기간비교 → 종목별 nearest date ≤ target.

### benchmark_daily_prices
cols: symbol | "date" | open | high | low | close | volume
symbols: KS11(KOSPI), SPY(S&P500), QQQ(NASDAQ100). stock_id 없이 symbol로 필터.

### financial_statements (long table)
cols: stock_id | account_id | year | quarter | accounting_date | value
규칙: wide table 아님. account_id는 아래 목록에서만 선택. fuzzy 검색 금지. latest period는 ROW_NUMBER로.

### financial_accounts
cols: id | account_name | account_type | created_at
주의: `name` 컬럼 없음, 반드시 `account_name` 사용.

### kr_investor_trade_daily
cols: stock_id | "date" | foreign_net_qty/value | personal_net_qty/value | institution_net_qty/value | securities_net_value | investment_trust_net_value | private_equity_fund_net_value | close | volume | price_change_pct
*_value 단위: 백만원. 수급은 반드시 이 테이블 조회.

### kr_program_trade_daily
cols: stock_id | "date" | program_buy/sell/net_qty/value | program_net_qty/value_change | close | volume | price_change_pct
*_value 단위: 백만원.

### kr_market_investor_daily
cols: market_code | "date" | foreign_net_qty/value | personal_net_value | institution_net_value | close | price_change_pct
*_value 단위: 백만원. 종목별 수급 → kr_investor_trade_daily.

### kr_market_program_daily
cols: market_code | "date" | whole/arbitrage/nonarbitrage_net_value/qty
*_value 단위: 백만원.

### stock_sectors
cols: stock_id | sector | sector_group | source | asof_date

### kr_loan_daily
cols: stock_id | "date" | new_loan_qty | redeem_qty | remaining_qty | remaining_amount(백만원) | remaining_qty_change

### kr_short_sale_daily
cols: stock_id | "date" | short_sale_qty/value | short_sale_volume_ratio | cumulative_short_sale_qty/value | short_sale_value_ratio
*_value 단위: 백만원.

### kr_stock_snapshots
cols: "date" | ticker | name | market | market_cap(백만원) | listed_shares | kospi200_sector | kospi100 | kospi50 | krx300 | kosdaq150 | market_cap_size | preferred
join: ticker → stocks.ticker. KOSPI200 편입 = kospi200_sector <> '0'. 데이터 시작: 2026-03-09.

## 섹터 목록

**KR** (sector 컬럼 값 — 정확한 이름 사용, 추측/탐색 쿼리 금지):
IT서비스, 가구, 가스유틸리티, 가정용기기와용품, 가정용품, 건강관리기술, 건강관리업체및서비스, 건강관리장비와용품, 건설, 건축자재, 건축제품, 게임엔터테인먼트, 광고, 교육서비스, 기계, 기타금융, 다각화된소비자서비스, 다각화된통신서비스, 담배, 도로와철도운송, 디스플레이장비및부품, 디스플레이패널, 레저용장비와제품, 무선통신서비스, 무역회사와판매업체, 문구류, 반도체와반도체장비, 방송과엔터테인먼트, 백화점과일반상점, 복합기업, 복합유틸리티, 부동산, 비철금속, 사무용전자제품, 상업서비스와공급품, 생명과학도구및서비스, 생명보험, 생물공학, 석유와가스, 섬유의류신발호화품, 소프트웨어, 손해보험, 식품, 식품과기본식료품소매, 양방향미디어와서비스, 에너지장비및서비스, 우주항공과국방, 운송인프라, 은행, 음료, 인터넷과카탈로그소매, 자동차, 자동차부품, 전기유틸리티, 전기장비, 전기제품, 전문소매, 전자장비와기기, 전자제품, 제약, 조선, 종이와목재, 증권, 창업투자, 철강, 출판, 카드, 컴퓨터와주변기기, 통신장비, 판매업체, 포장재, 항공사, 항공화물운송과물류, 해운사, 핸드셋, 호텔레스토랑레저, 화장품, 화학

**US**: Communication Services, Consumer Discretionary, Consumer Staples, Energy, Financials, Health Care, Industrials, Information Technology, Materials, Real Estate, Utilities

참고: "방산"=우주항공과국방, "로봇"=섹터에 없음(종목명 검색), "2차전지/배터리"=전기장비, "바이오"=생물공학 또는 제약

## KR account ID (kis_kr)

6576 grs | 6577 bsop_prfi_inrt | 6578 ntin_inrt | 6579 roe_val | 6580 eps | 6581 sps | 6582 bps | 6583 rsrv_rate | 6584 lblt_rate | 6585 bram_depn | 6586 crnt_rate | 6587 quck_rate | 6588 payout_rate | 6589 eva | 6590 ebitda | 6591 ev_ebitda | 6592 sale_account(매출) | 6593 sale_cost | 6594 sale_totl_prfi(매출총이익) | 6595 depr_cost | 6596 sell_mang | 6597 bsop_prti(영업이익) | 6598 bsop_non_ernn | 6599 bsop_non_expn | 6600 op_prfi | 6601 spec_prfi | 6602 spec_loss | 6603 thtr_ntin(순이익) | 6604 cras | 6605 fxas | 6606 total_aset | 6607 flow_lblt | 6608 fix_lblt | 6609 total_lblt | 6610 cpfn | 6611 cfp_surp | 6612 prfi_surp | 6613 total_cptl | 6614 cptl_ntin_rate | 6615 self_cptl_ntin_inrt | 6616 sale_ntin_rate | 6617 sale_totl_rate | 6618 equt_inrt | 6619 totl_aset_inrt

reliability: trusted raw flow=6592,6597,6603,6594,6590 | trusted raw point=6580,6581,6582 | caution=6579,6584,6591 | recompute=6576,6577,6578

## US account ID (stockanalysis, 주요 항목)

131 Revenue | 132 Revenue Growth (YoY) | 134 Gross Profit | 135 Selling, General & Admin | 136 Research & Development | 138 Operating Income | 139 Interest Expense | 143 Pretax Income | 145 Net Income | 147 Net Income Growth | 148 Shares Outstanding (Basic) | 149 Shares Outstanding (Diluted) | 151 EPS (Basic) | 152 EPS (Diluted) | 153 EPS Growth | 154 Free Cash Flow | 158 Gross Margin | 159 Operating Margin | 160 Profit Margin | 162 EBITDA | 169 Cash & Equivalents | 178 Total Current Assets | 183 Total Assets | 192 Total Current Liabilities | 196 Total Liabilities | 200 Shareholders' Equity | 202 Total Debt | 209 Book Value Per Share | 223 Operating Cash Flow | 225 Capital Expenditures

위 목록에 없는 US account는 `financial_accounts` 테이블에서 account_type='stockanalysis'로 조회.

## 파생 지표

PBR = close / bps(6582) | PER = close / eps(6580) | OpMargin = bsop_prti(6597) / sale_account(6592) | Revenue_YoY = 6592 전년동기비 | OpIncome_YoY = 6597 전년동기비 | NetIncome_YoY = 6603 전년동기비 | 5Y_OpIncome_CAGR = (latest/5yr_ago)^(1/5)-1

## Canonical SQL 패턴

### latest point metric (per stock)
```sql
select stock_id, value from (
  select fs.stock_id, fs.value,
    row_number() over (partition by fs.stock_id order by fs.year desc, fs.quarter desc) as rn
  from financial_statements fs where fs.account_id = :ACCOUNT_ID
) where rn = 1
```

### latest close (per stock)
```sql
select stock_id, close from (
  select dp.stock_id, dp.close,
    row_number() over (partition by dp.stock_id order by dp."date" desc) as rn
  from daily_prices dp
) where rn = 1
```

### 등락률 (LAG 패턴)
```sql
select stock_id, "date", close,
  round((close - lag(close) over (partition by stock_id order by "date")) /
    nullif(lag(close) over (partition by stock_id order by "date"), 0) * 100, 2) as pct
from daily_prices where "date" >= TO_DATE(:START,'YYYY-MM-DD')
```
