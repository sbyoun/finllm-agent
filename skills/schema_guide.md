# Schema Guide

이 문서는 금융 SQL 작업에 쓰는 단일 schema skill이다.
DB 질문이면 이 문서를 먼저 읽고, 여기 있는 규칙과 패턴만 기준으로 SQL을 작성한다.

## 핵심 원칙

- `stocks`를 종목 식별의 앵커 테이블로 사용한다.
- `stock_id`는 상상하지 말고 `stocks.id`를 통해 얻는다.
- 문서에 없는 컬럼은 존재한다고 추정하지 않는다.
- 핵심 metric이 스키마에 없거나 여기 적힌 파생 규칙으로 계산 불가능하면 SQL보다 `search_news` 또는 unavailable 안내가 우선이다.
- 핵심 metric이 없다고 해서 proxy metric으로 대체하지 않는다.
- `market_cap`이 없다고 `close * volume`으로 대체하지 않는다.
- `market share`가 없다고 가격, 거래량, 매출 순위로 대체하지 않는다.
- `brand ranking`이 없다고 기사 수나 검색 결과 수로 대체하지 않는다.
- SQL을 쓰기 전, 질문의 required conditions, metrics, ranking, filters를 짧게라도 내부적으로 분해한 뒤 쿼리에 반영한다.
- 구현 불가능한 조건은 조용히 약화하지 말고 explicitly unavailable로 처리한다.
- `실적`, `밸류`, `수급`처럼 넓은 표현은 기본적으로 다의적이라고 본다.
- 이런 넓은 표현은 가능하면 대표 단일 metric 하나로 축소하지 말고, 주요 관련 metric을 함께 포함하는 결과를 우선한다.
- 예:
  - `실적` -> 매출, 영업이익, 순이익
  - `밸류` -> PER, PBR, EV/EBITDA
  - `수급` -> 주요 투자주체 순매수 관련 metric들

## SQL 사용 규칙

- schema는 툴이 아니다. 이 문서를 읽고 바로 SQL을 작성한다.
- SQL 실패를 summary로 덮지 않는다.
- SQL 에러가 나면 같은 분석 목표를 유지한 채 SQL을 수정해서 다시 시도한다.
- 이미 성공한 동일 SQL을 반복 실행하지 않는다. 답변하거나 materially different query만 다시 실행한다.
- 질문 조건이 실제 SQL에 들어갔는지 `select`, `where`, `join`, `order by` 기준으로 점검한다.
- `낮은/높은/상위/하위` 같은 방향성 표현은 SQL의 `where` 또는 `order by`에 들어가야 한다.
- 최종 observation에 없는 metric이나 filter를 답변에서 이미 충족한 것처럼 말하지 않는다.
- observation이 단순 후보 리스트면 `저평가 확정`처럼 과한 결론을 말하지 않는다.
- 결과가 근사치, 후보군, 보조 지표 수준이면 그 수준에 맞게 표현한다.

## Oracle SQL hygiene

- SQL 끝에 세미콜론(`;`)을 붙이지 않는다.
- `LIMIT`를 쓰지 않는다.
- row 제한은 `FETCH FIRST ... ROWS ONLY`, `OFFSET ... FETCH`, 또는 `rownum`을 사용한다.
- `daily_prices`의 날짜 컬럼은 항상 `\"date\"`를 사용한다.
- 날짜 비교에서 문자열 literal을 직접 비교하지 않는다.
- 날짜 상수는 항상 `TO_DATE('YYYY-MM-DD', 'YYYY-MM-DD')` 형태로 명시한다.
- 예: `daily_prices."date" >= TO_DATE('2025-01-02', 'YYYY-MM-DD')`
- 날짜 arithmetic이 필요하면 날짜 타입끼리 계산한다. 문자열 날짜에서 빼기/비교를 하지 않는다.
- `daily_prices."date"`와 비교하는 기준일도 Oracle date expression으로 맞춘다.
- inline view / CTE alias에서 컬럼명을 다시 감쌀 때 불필요한 표현을 만들지 않는다.

## 주요 테이블

### `stocks`

종목 식별의 기준 테이블이다.

주요 컬럼:

- `id`
- `ticker`
- `name`
- `country`
- `market`

용도:

- 종목명 -> ticker 확인
- ticker -> 내부 `stock_id` 확인
- KR / US / KOSPI / KOSDAQ 구분

규칙:

- 가격/재무 테이블에 바로 종목명을 넣지 말고, 먼저 `stocks`에서 대상 종목을 잡는다.
- `stock_id`는 상상하지 말고 `stocks.id`를 사용한다.

예시:

```sql
with target_stocks as (
  select id, ticker, name, country, market
  from stocks
  where ticker in ('NVDA', '005930')
)
select *
from target_stocks
```

```sql
select id, ticker, name, country, market
from stocks
where lower(name) like lower('%엔비디아%')
   or lower(name) like lower('%nvidia%')
```

### `daily_prices`

일별 가격 테이블이다.

주요 컬럼:

- `stock_id`
- `date`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `created_at`

용도:

- 최신 종가
- 특정 기간 가격 비교
- 수익률 계산

규칙:

- 날짜 컬럼은 항상 `daily_prices."date"`를 사용한다.
- `daily_prices`에는 `trading_date`가 없다.
- `daily_prices`에는 `pbr`, `per`, `market_cap`, `roe`, `dividend_yield` 같은 컬럼이 없다고 가정한다.
- 시가총액이 필요하다고 해서 `close * volume`으로 대체하지 않는다.
- 기간 비교나 수익률 계산에서 목표 날짜와 `daily_prices."date"`의 정확한 일치에 의존하지 않는다.
- 목표 날짜에 거래 데이터가 없을 수 있으므로, 각 종목별로 그 날짜 이하의 가장 최근 거래일을 기준점으로 잡는다.
- 전역 기준일 하나를 exact match로 모든 종목에 강제하지 않는다.
- 기간 비교는 "해당 기준일 직전의 가장 가까운 관측치"를 찾는 방식으로 작성한다.

조인 예시:

```sql
select s.ticker, p."date", p.close
from stocks s
join daily_prices p on p.stock_id = s.id
```

### `financial_statements`

`financial_statements`는 `account_id` 기반 long table이다.

실제 구조:

- `stock_id`
- `account_id`
- `year`
- `quarter`
- `accounting_date`
- `value`

규칙:

- wide table로 가정하지 않는다.
- line item은 `financial_accounts` 또는 알려진 `account_id`로 해석한다.
- `financial_accounts` 테이블의 컬럼은 `id`, `account_name`, `account_type`, `created_at`이다. `name` 컬럼은 존재하지 않는다. 반드시 `account_name`을 사용한다.
- account 선택은 아래의 시장별 full account id list를 authoritative source로 사용한다.
- KR 질문이면 `kis_kr` full list 안에서만 고른다.
- US 질문이면 `stockanalysis` full list 안에서만 고른다.
- 사용자가 schema 탐색 자체를 요청한 경우가 아니면 `financial_accounts`를 `account_name like '%...%'`로 fuzzy 검색해서 metric을 찾지 않는다.
- `quarter = 0`을 canonical annual 값으로 가정하지 않는다.
- latest period는 stock/account별로 SQL로 찾는다.

`quarter` 해석:

- 보통 `1`, `2`, `3`, `4`가 존재한다.
- 일부 계열에서 `0`이 보여도 표준 annual 값이라고 단정하지 않는다.
- comparable period가 필요하면 같은 quarter를 맞춰서 비교한다.

## KR full account id list

KR financial metric은 `financial_accounts.account_type = 'kis_kr'`만 기준으로 본다.

- `6576 grs`
- `6577 bsop_prfi_inrt`
- `6578 ntin_inrt`
- `6579 roe_val`
- `6580 eps`
- `6581 sps`
- `6582 bps`
- `6583 rsrv_rate`
- `6584 lblt_rate`
- `6585 bram_depn`
- `6586 crnt_rate`
- `6587 quck_rate`
- `6588 payout_rate`
- `6589 eva`
- `6590 ebitda`
- `6591 ev_ebitda`
- `6592 sale_account`
- `6593 sale_cost`
- `6594 sale_totl_prfi`
- `6595 depr_cost`
- `6596 sell_mang`
- `6597 bsop_prti`
- `6598 bsop_non_ernn`
- `6599 bsop_non_expn`
- `6600 op_prfi`
- `6601 spec_prfi`
- `6602 spec_loss`
- `6603 thtr_ntin`
- `6604 cras`
- `6605 fxas`
- `6606 total_aset`
- `6607 flow_lblt`
- `6608 fix_lblt`
- `6609 total_lblt`
- `6610 cpfn`
- `6611 cfp_surp`
- `6612 prfi_surp`
- `6613 total_cptl`
- `6614 cptl_ntin_rate`
- `6615 self_cptl_ntin_inrt`
- `6616 sale_ntin_rate`
- `6617 sale_totl_rate`
- `6618 equt_inrt`
- `6619 totl_aset_inrt`

## US full account id list

US financial metric은 `financial_accounts.account_type = 'stockanalysis'`만 기준으로 본다.

- `131 Revenue`
- `132 Revenue Growth (YoY)`
- `133 Cost of Revenue`
- `134 Gross Profit`
- `135 Selling, General & Admin`
- `136 Research & Development`
- `137 Operating Expenses`
- `138 Operating Income`
- `139 Interest Expense`
- `140 Interest & Investment Income`
- `141 Other Non Operating Income (Expenses)`
- `142 EBT Excluding Unusual Items`
- `143 Pretax Income`
- `144 Income Tax Expense`
- `145 Net Income`
- `146 Net Income to Common`
- `147 Net Income Growth`
- `148 Shares Outstanding (Basic)`
- `149 Shares Outstanding (Diluted)`
- `150 Shares Change (YoY)`
- `151 EPS (Basic)`
- `152 EPS (Diluted)`
- `153 EPS Growth`
- `154 Free Cash Flow`
- `155 Free Cash Flow Per Share`
- `156 Dividend Per Share`
- `157 Dividend Growth`
- `158 Gross Margin`
- `159 Operating Margin`
- `160 Profit Margin`
- `161 Free Cash Flow Margin`
- `162 EBITDA`
- `163 EBITDA Margin`
- `164 D&A For EBITDA`
- `165 EBIT`
- `166 EBIT Margin`
- `167 Effective Tax Rate`
- `168 Revenue as Reported`
- `169 Cash & Equivalents`
- `170 Short-Term Investments`
- `171 Cash & Short-Term Investments`
- `172 Cash Growth`
- `173 Accounts Receivable`
- `174 Other Receivables`
- `175 Receivables`
- `176 Inventory`
- `177 Other Current Assets`
- `178 Total Current Assets`
- `179 Property, Plant & Equipment`
- `180 Long-Term Investments`
- `181 Long-Term Deferred Tax Assets`
- `182 Other Long-Term Assets`
- `183 Total Assets`
- `184 Accounts Payable`
- `185 Accrued Expenses`
- `186 Short-Term Debt`
- `187 Current Portion of Long-Term Debt`
- `188 Current Portion of Leases`
- `189 Current Income Taxes Payable`
- `190 Current Unearned Revenue`
- `191 Other Current Liabilities`
- `192 Total Current Liabilities`
- `193 Long-Term Debt`
- `194 Long-Term Leases`
- `195 Other Long-Term Liabilities`
- `196 Total Liabilities`
- `197 Common Stock`
- `198 Retained Earnings`
- `199 Comprehensive Income & Other`
- `200 Shareholders' Equity`
- `201 Total Liabilities & Equity`
- `202 Total Debt`
- `203 Net Cash (Debt)`
- `204 Net Cash Growth`
- `205 Net Cash Per Share`
- `206 Filing Date Shares Outstanding`
- `207 Total Common Shares Outstanding`
- `208 Working Capital`
- `209 Book Value Per Share`
- `210 Tangible Book Value`
- `211 Tangible Book Value Per Share`
- `212 Land`
- `213 Machinery`
- `214 Leasehold Improvements`
- `215 Depreciation & Amortization`
- `216 Stock-Based Compensation`
- `217 Other Operating Activities`
- `218 Change in Accounts Receivable`
- `219 Change in Inventory`
- `220 Change in Accounts Payable`
- `221 Change in Unearned Revenue`
- `222 Change in Other Net Operating Assets`
- `223 Operating Cash Flow`
- `224 Operating Cash Flow Growth`
- `225 Capital Expenditures`
- `226 Cash Acquisitions`
- `227 Investment in Securities`
- `228 Other Investing Activities`
- `229 Investing Cash Flow`
- `230 Total Debt Issued`
- `231 Short-Term Debt Repaid`
- `232 Long-Term Debt Repaid`
- `233 Total Debt Repaid`
- `234 Net Debt Issued (Repaid)`
- `235 Repurchase of Common Stock`
- `236 Common Dividends Paid`
- `237 Other Financing Activities`
- `238 Financing Cash Flow`
- `239 Net Cash Flow`
- `240 Free Cash Flow Growth`
- `241 Cash Interest Paid`
- `242 Cash Income Tax Paid`
- `243 Levered Free Cash Flow`
- `244 Unlevered Free Cash Flow`
- `245 Change in Working Capital`
- `11342 Issuance of Common Stock`
- `11343 Other Intangible Assets`
- `11344 Total Common Equity`
- `11345 Other Unusual Items`
- `11346 Prepaid Expenses`
- `11347 Long-Term Deferred Tax Liabilities`
- `11348 Merger & Restructuring Charges`
- `11349 Change in Income Taxes`
- `11350 Additional Paid-In Capital`
- `11351 Goodwill`
- `11352 Restricted Cash`
- `11353 Currency Exchange Gain (Loss)`
- `11354 Other Amortization`
- `11355 Long-Term Debt Issued`
- `11356 Foreign Exchange Rate Adjustments`
- `11357 Provision & Write-off of Bad Debts`
- `11555 Treasury Stock`
- `11556 Asset Writedown`
- `11557 Finance Div. Debt Long-Term`
- `11558 Divestitures`
- `11559 Earnings From Discontinued Operations`
- `11560 Net Income to Company`
- `11561 Earnings From Equity Investments`
- `11562 Long-Term Deferred Charges`
- `11563 Buildings`
- `11564 Asset Writedown & Restructuring Costs`
- `11565 Dividends Paid`
- `11566 Sale (Purchase) of Intangibles`
- `11567 Short-Term Debt Issued`
- `11568 Finance Div. Other Current Assets`
- `11569 Finance Div. Other Long-Term Liabilities`
- `11570 Finance Div. Debt Current`
- `11571 Long-Term Unearned Revenue`
- `11572 Operating Revenue`
- `11573 Minority Interest in Earnings`
- `11574 Earnings From Continuing Operations`
- `11575 Loss (Gain) From Sale of Assets`
- `11576 Sale of Property, Plant & Equipment`
- `11577 Preferred Dividends & Other Adjustments`
- `11578 Finance Div. Other Current Liabilities`
- `11579 Minority Interest`
- `11580 Other Revenue`
- `11581 Miscellaneous Cash Flow Adjustments`
- `11582 Finance Div. Loans and Leases Long-Term`
- `11583 Finance Div. Loans and Leases`
- `11584 Loss (Gain) on Equity Investments`
- `11585 Gain (Loss) on Sale of Assets`
- `11586 Impairment of Goodwill`
- `11587 Loss (Gain) From Sale of Investments`
- `11588 Sale (Purchase) of Real Estate`
- `11589 Construction In Progress`
- `11590 Pension & Post-Retirement Benefits`
- `11591 Long-Term Accounts Receivable`
- `11592 Legal Settlements`
- `11593 Amortization of Goodwill & Intangibles`
- `11594 Trading Asset Securities`
- `11595 Advertising Expenses`
- `11596 Earnings From Continuing Ops.`
- `11597 Premiums & Annuity Revenue`
- `11598 Deferred Policy Acquisition Cost`
- `11599 Total Investments`
- `11600 Total Revenue`
- `11601 Change in Insurance Reserves / Liabilities`
- `11602 Investments in Debt Securities`
- `11603 Selling, General & Administrative`
- `11604 Repurchases of Common Stock`
- `11605 Other Operating Expenses`
- `11606 Insurance & Annuity Liabilities`
- `11607 Other Investments`
- `11608 Unearned Premiums`
- `11609 Policy Acquisition & Underwriting Costs`
- `11610 Investments in Equity & Preferred Securities`
- `11611 Total Interest & Dividend Income`
- `11612 Policy Loans`
- `11613 Policy Benefits`
- `11614 Total Operating Expenses`
- `11615 Unpaid Claims`
- `11616 Funds From Operations (FFO)`
- `11617 Revenue Growth (YoY`
- `11618 D&A For Ebitda`
- `11619 Acquisition of Real Estate Assets`
- `11620 Investment In Debt and Equity Securities`
- `11621 Net Sale / Acq. of Real Estate Assets`
- `11622 Sale of Real Estate Assets`
- `11623 Rental Revenue`
- `11624 FFO Payout Ratio`
- `11625 Provision for Loan Losses`
- `11626 AFFO Per Share`
- `11627 Adjusted Funds From Operations (AFFO)`
- `11628 FFO Per Share`
- `11629 Investment in Marketable & Equity Securities`
- `11630 Diluted Shares Outstanding`
- `11631 Loans Receivable Current`
- `11632 Deferred Long-Term Tax Assets`
- `11633 Property Expenses`
- `11634 Total Insurance Settlements`
- `11635 Other Non-Operating Income`
- `11636 Basic Shares Outstanding`
- `11637 Preferred Share Repurchases`
- `11638 Deferred Long-Term Charges`
- `11639 Preferred Dividends Paid`
- `11640 Cash Acquisition`
- `11641 Total Legal Settlements`
- `11642 Preferred Stock, Redeemable`
- `11643 Total Dividends Paid`
- `11644 Preferred Stock Issued`
- `11645 Net Cash from Discontinued Operations`
- `11646 Separate Account Assets`
- `11647 Separate Account Liability`
- `11648 Reinsurance Recoverable`
- `11649 Earnings From Discontinued Ops.`
- `11650 Order Backlog`
- `11651 Interest and Dividend Income`
- `11652 Revenue Before Loan Losses`
- `11653 Depreciation & Amortization, Total`
- `11654 Investments in Debt & Equity Securities`
- `11655 Trading & Principal Transactions`
- `11656 Brokerage Commission`
- `11657 Asset Management Fee`
- `11658 Net Interest Income`
- `11659 Salaries & Employee Benefits`
- `11660 Cost of Services Provided`
- `11661 Total Interest Expense`
- `11662 Underwriting & Investment Banking Fee`
- `11663 Distributions in Excess of Earnings`
- `11664 Total Non-Interest Expense`
- `11665 Short-Term Borrowings`
- `11666 Salaries and Employee Benefits`
- `11667 Total Deposits`
- `11668 Mortgage-Backed Securities`
- `11669 Net Increase (Decrease) in Deposit Accounts`
- `11670 Net Loans`
- `11671 Total Asset Writedown`
- `11672 Revenues Before Loan Losses`
- `11673 Accrued Interest Payable`
- `11674 Total Interest Income`
- `11675 Net Decrease (Increase) in Loans Originated / Sold - Operating`
- `11676 Allowance for Loan Losses`
- `11677 Federal Home Loan Bank Debt, Long-Term`
- `11678 Gross Loans`
- `11679 Occupancy Expenses`
- `11680 Sale of Property, Plant and Equipment`
- `11681 Total Non-Interest Income`
- `11682 Interest Income on Loans`
- `11683 Interest Bearing Deposits`
- `11684 Non-Interest Income Growth (YoY)`
- `11685 Other Non-Interest Income`
- `11686 Net Decrease (Increase) in Loans Originated / Sold - Investing`
- `11687 Interest Paid on Deposits`
- `11688 Investment Securities`
- `11689 Interest Income on Investments`
- `11690 Other Real Estate Owned & Foreclosed`
- `11691 Other Non-Interest Expense`
- `11692 Trust Preferred Securities`
- `11693 Loans Held for Sale`
- `11694 Non-Interest Bearing Deposits`
- `11695 Interest Paid on Borrowings`

## KR reliability policy

- trusted raw flow:
  - `6592`, `6597`, `6603`, `6594`, `6590`
- trusted raw point:
  - `6580`, `6581`, `6582`
- use with caution:
  - `6579`, `6584`, `6591`
- do not use directly, recompute:
  - `6576 grs`
  - `6577 bsop_prfi_inrt`
  - `6578 ntin_inrt`

## 파생 지표

기본 규칙:

- `PBR`, `PER`, `YoY`, `CAGR`, `Margin`은 직접 계산할 수 있다.
- 문서에 없는 컬럼을 상상해서 쓰지 않는다.
- 필요한 원재료 line item은 `financial_statements`의 trusted account를 사용한다.

대표 파생 지표:

- `PBR = latest close / latest bps(6582)`
- `PER = latest close / latest eps(6580)`
- `Revenue_YoY = sale_account(6592)`의 전년동기 대비 재계산
- `Operating Income_YoY = bsop_prti(6597)`의 전년동기 대비 재계산
- `Net Income_YoY = thtr_ntin(6603)`의 전년동기 대비 재계산
- `Operating Margin = bsop_prti(6597) / sale_account(6592)`
- `5Y operating income CAGR = latest comparable bsop_prti / 5년 전 같은 quarter bsop_prti`

## Canonical SQL patterns

### stock별 latest point metric

```sql
with latest_point_metric as (
  select stock_id, value
  from (
    select
      fs.stock_id,
      fs.value,
      fs.year,
      fs.quarter,
      row_number() over (
        partition by fs.stock_id
        order by fs.year desc, fs.quarter desc
      ) as rn
    from financial_statements fs
    where fs.account_id = 6582
  )
  where rn = 1
)
```

### stock별 latest close

```sql
with latest_close as (
  select stock_id, close
  from (
    select
      dp.stock_id,
      dp.close,
      dp."date",
      row_number() over (
        partition by dp.stock_id
        order by dp."date" desc
      ) as rn
    from daily_prices dp
  )
  where rn = 1
)
```

### stock별 latest comparable flow period

```sql
with latest_flow_period as (
  select stock_id, year, quarter, value
  from (
    select
      fs.stock_id,
      fs.year,
      fs.quarter,
      fs.value,
      row_number() over (
        partition by fs.stock_id
        order by fs.year desc, fs.quarter desc
      ) as rn
    from financial_statements fs
    where fs.account_id = 6597
  )
  where rn = 1
)
```

### 5년 전 같은 quarter 연결

```sql
with base_flow_period as (
  select
    lfp.stock_id,
    fs.year,
    fs.quarter,
    fs.value
  from latest_flow_period lfp
  join financial_statements fs
    on fs.stock_id = lfp.stock_id
  where fs.account_id = 6597
    and fs.year = lfp.year - 5
    and fs.quarter = lfp.quarter
)
```

### 5년 영업이익 CAGR

```sql
with latest_op_period as (
  select stock_id, year, quarter, value as latest_op
  from (
    select
      fs.stock_id,
      fs.year,
      fs.quarter,
      fs.value,
      row_number() over (
        partition by fs.stock_id
        order by fs.year desc, fs.quarter desc
      ) as rn
    from financial_statements fs
    where fs.account_id = 6597
  )
  where rn = 1
),
base_op_period as (
  select
    lop.stock_id,
    fs.year,
    fs.quarter,
    fs.value as base_op
  from latest_op_period lop
  join financial_statements fs
    on fs.stock_id = lop.stock_id
  where fs.account_id = 6597
    and fs.year = lop.year - 5
    and fs.quarter = lop.quarter
)
select
  lop.stock_id,
  round((power(lop.latest_op / nullif(bop.base_op, 0), 1.0 / 5.0) - 1) * 100, 2) as oi_cagr_5y_pct
from latest_op_period lop
join base_op_period bop on bop.stock_id = lop.stock_id
where lop.latest_op > 0 and bop.base_op > 0
```

## 예시 쿼리

### line item pivot

```sql
with target_stock as (
  select id, ticker, name
  from stocks
  where ticker = '005930'
),
base as (
  select
    s.ticker,
    s.name,
    f.year,
    f.quarter,
    f.account_id,
    f.value
  from target_stock s
  join financial_statements f
    on f.stock_id = s.id
  where f.account_id in (6592, 6597, 6579, 6584)
)
select
  ticker,
  name,
  year,
  quarter,
  max(case when account_id = 6592 then value end) as revenue,
  max(case when account_id = 6597 then value end) as operating_income,
  max(case when account_id = 6579 then value end) as roe,
  max(case when account_id = 6584 then value end) as debt_ratio
from base
group by ticker, name, year, quarter
order by year desc, quarter desc
```

### KR 최신 PBR

```sql
with latest_bps as (
  select stock_id, value as bps
  from (
    select
      fs.stock_id,
      fs.value,
      fs.year,
      fs.quarter,
      row_number() over (
        partition by fs.stock_id
        order by fs.year desc, fs.quarter desc
      ) as rn
    from financial_statements fs
    where fs.account_id = 6582
  )
  where rn = 1
),
latest_close as (
  select stock_id, close
  from (
    select
      dp.stock_id,
      dp.close,
      dp."date",
      row_number() over (
        partition by dp.stock_id
        order by dp."date" desc
      ) as rn
    from daily_prices dp
  )
  where rn = 1
)
select
  lc.stock_id,
  round(lc.close / nullif(lb.bps, 0), 4) as pbr
from latest_close lc
join latest_bps lb on lb.stock_id = lc.stock_id
where lb.bps > 0
```

### KR 최신 PER

```sql
with latest_eps as (
  select stock_id, value as eps
  from (
    select
      fs.stock_id,
      fs.value,
      fs.year,
      fs.quarter,
      row_number() over (
        partition by fs.stock_id
        order by fs.year desc, fs.quarter desc
      ) as rn
    from financial_statements fs
    where fs.account_id = 6580
  )
  where rn = 1
),
latest_close as (
  select stock_id, close
  from (
    select
      dp.stock_id,
      dp.close,
      dp."date",
      row_number() over (
        partition by dp.stock_id
        order by dp."date" desc
      ) as rn
    from daily_prices dp
  )
  where rn = 1
)
select
  lc.stock_id,
  round(lc.close / nullif(le.eps, 0), 4) as per
from latest_close lc
join latest_eps le on le.stock_id = lc.stock_id
where le.eps > 0
```
