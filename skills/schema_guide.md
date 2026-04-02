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
- `quarter = 0`을 canonical annual 값으로 가정하지 않는다.
- latest period는 stock/account별로 SQL로 찾는다.

`quarter` 해석:

- 보통 `1`, `2`, `3`, `4`가 존재한다.
- 일부 계열에서 `0`이 보여도 표준 annual 값이라고 단정하지 않는다.
- comparable period가 필요하면 같은 quarter를 맞춰서 비교한다.

## KR 주요 account id

- `6592 sale_account`: 매출액
- `6597 bsop_prti`: 영업이익
- `6603 thtr_ntin`: 순이익
- `6579 roe_val`: ROE
- `6580 eps`: EPS
- `6581 sps`: SPS
- `6582 bps`: BPS
- `6584 lblt_rate`: 부채비율
- `6590 ebitda`: EBITDA
- `6591 ev_ebitda`: EV/EBITDA
- `6594 sale_totl_prfi`: 매출총이익
- `6606 total_aset`: 총자산
- `6609 total_lblt`: 총부채
- `6613 total_cptl`: 자기자본

## US 주요 account id

- `131`: Revenue
- `138`: Operating Income
- `145`: Net Income
- `151`: EPS (Basic)
- `152`: EPS (Diluted)
- `162`: EBITDA
- `183`: Total Assets
- `196`: Total Liabilities
- `200`: Shareholders' Equity
- `202`: Total Debt
- `209`: Book Value Per Share
- `223`: Operating Cash Flow

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
