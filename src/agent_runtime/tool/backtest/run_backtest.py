"""run_backtest tool: backtest a factor-based strategy using historical data.

The agent writes a screening SQL with an {as_of_date} placeholder.
The engine substitutes the rebalancing date for each period and calculates returns.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition
from agent_runtime.tool.sql.oracle import OracleSQLRunner


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

_KR_UNIVERSES = {"KOSPI", "KOSDAQ"}
_US_UNIVERSES = {"SP500", "NASDAQ"}

# ---------------------------------------------------------------------------
# Look-ahead bias: rebalancing schedule per market
# KR: Apr/Jun/Sep/Dec — based on kr_financial_timing.py publication lags
# US: Mar/Jun/Sep/Dec — 2-month lag after quarter end (us_model_builder.py)
# ---------------------------------------------------------------------------
_KR_REBALANCE_SCHEDULE = [(4,), (6,), (9,), (12,)]
_US_REBALANCE_SCHEDULE = [(3,), (6,), (9,), (12,)]

_BACKTEST_REQUEST_RE = re.compile(
    r"(백테스트|backtest|과거\s*성과|성과\s*검증|누적\s*수익|mdd|cagr|샤프|sharpe|시뮬레이션)",
    re.IGNORECASE,
)

_LOOKBACK_PRICE_ANCHOR_RE = re.compile(
    r"(?P<date_expr>(?P<price_alias>[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*\"date\")"
    r"\s+between\s+"
    r"(?P<anchor>(?:[A-Za-z_][A-Za-z0-9_$]*\s*\.\s*)?m(?:1|6|9|12|24|36)_dt)"
    r"\s+and\s+"
    r"(?P<ref>(?:[A-Za-z_][A-Za-z0-9_$]*\s*\.\s*)?ref_dt)\b",
    re.IGNORECASE,
)

_GLOBAL_PRICE_DATE_ANCHOR_RE = re.compile(
    r"\(\s*select\s+max\s*\(\s*(?:(?P<max_alias>[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*)?\"date\"\s*\)"
    r"\s+from\s+daily_prices(?:\s+(?!where\b)(?P<table_alias>[A-Za-z_][A-Za-z0-9_$]*))?"
    r"\s+where\s+",
    re.IGNORECASE,
)

_DATE_LE_PREDICATE_RE = re.compile(
    r"^\s*(?:(?P<alias>[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*)?\"date\"\s*<=\s*(?P<target>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _latest_user_message(conversation: Any) -> str:
    state = getattr(conversation, "state", None)
    event_log = getattr(state, "event_log", None)
    if event_log is None:
        return ""
    for event in reversed(list(event_log)):
        if getattr(event, "role", None) == "user":
            return str(getattr(event, "content", "") or "")
    return ""


def _explicitly_requests_backtest(conversation: Any) -> bool:
    return bool(_BACKTEST_REQUEST_RE.search(_latest_user_message(conversation)))


def _rebalance_schedule(universe: str) -> list[tuple[int]]:
    return _KR_REBALANCE_SCHEDULE if universe in _KR_UNIVERSES else _US_REBALANCE_SCHEDULE


def _benchmark_symbol(universe: str) -> str:
    return "KS11" if universe in _KR_UNIVERSES else "SPY"


def _find_matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    in_single_quote = False
    in_double_quote = False
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double_quote:
            if in_single_quote and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif not in_single_quote and not in_double_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _universe_stock_predicate(stock_alias: str, universe: str | None) -> str | None:
    normalized_universe = (universe or "").upper()
    if normalized_universe in _KR_UNIVERSES:
        return (
            f"{stock_alias}.country = 'KR' "
            f"AND {stock_alias}.market = '{normalized_universe}' "
            f"AND {stock_alias}.instrument_type = 'stock'"
        )
    if normalized_universe == "SP500":
        return (
            f"{stock_alias}.country = 'US' "
            f"AND {stock_alias}.market = 'SP500' "
            f"AND {stock_alias}.instrument_type = 'stock'"
        )
    if normalized_universe == "NASDAQ":
        return (
            f"{stock_alias}.country = 'US' "
            f"AND {stock_alias}.market = 'NASDAQ' "
            f"AND {stock_alias}.instrument_type = 'stock'"
        )
    return None


def _normalize_global_price_date_anchors(screening_sql: str, universe: str | None) -> tuple[str, list[str]]:
    """Scope global daily_prices MAX(date) anchors to the requested universe.

    Without this, a KR backtest can pick a non-KR trading date from another market.
    That date then has zero KOSPI rows, causing empty monthly holdings.
    """
    stock_predicate = _universe_stock_predicate("fa_market_stock", universe)
    if not stock_predicate:
        return screening_sql, []

    notes: list[str] = []
    parts: list[str] = []
    cursor = 0

    for match in _GLOBAL_PRICE_DATE_ANCHOR_RE.finditer(screening_sql):
        if match.start() < cursor:
            continue
        close_index = _find_matching_paren(screening_sql, match.start())
        if close_index is None:
            continue

        where_clause = screening_sql[match.end():close_index].strip()
        lowered_where = where_clause.lower()
        if any(token in lowered_where for token in ("stock_id", "country", "market", "instrument_type", "ticker", "symbol")):
            continue

        predicate_match = _DATE_LE_PREDICATE_RE.match(where_clause)
        if not predicate_match:
            continue

        target_expr = predicate_match.group("target").strip()
        compact_target = re.sub(r"\s+", "", target_expr)
        parts.append(screening_sql[cursor:match.start()])
        parts.append(
            "("
            "SELECT MAX(fa_market_dp.\"date\") "
            "FROM daily_prices fa_market_dp "
            "JOIN stocks fa_market_stock ON fa_market_stock.id = fa_market_dp.stock_id "
            f"WHERE {stock_predicate} "
            f"AND fa_market_dp.\"date\" <= {target_expr}"
            ")"
        )
        notes.append(f"global MAX(daily_prices.date <= {compact_target}) -> {universe} universe MAX(date <= {compact_target})")
        cursor = close_index + 1

    if not parts:
        return screening_sql, []

    parts.append(screening_sql[cursor:])
    return "".join(parts), notes


def _normalize_screening_sql_date_anchors(screening_sql: str, universe: str | None = None) -> tuple[str, list[str]]:
    """Force lookback price anchors to use the previous trading day.

    LLM-generated screening SQL sometimes searches a price anchor with:
      price."date" BETWEEN m12_dt AND ref_dt
    That finds the first trading day after m12_dt when m12_dt is a holiday.
    For return lookbacks the invariant is the latest trading day <= m12_dt.
    """
    notes: list[str] = []

    def replace(match: re.Match[str]) -> str:
        date_expr = match.group("date_expr")
        price_alias = match.group("price_alias")
        anchor = match.group("anchor")
        ref = match.group("ref")
        normalized_anchor = re.sub(r"\s+", "", anchor)
        normalized_ref = re.sub(r"\s+", "", ref)
        notes.append(
            f"{date_expr} BETWEEN {normalized_anchor} AND {normalized_ref} -> "
            f"same-stock MAX(date <= {normalized_anchor})"
        )
        return (
            f"{date_expr} = ("
            f"SELECT MAX(fa_anchor_dp.\"date\") "
            f"FROM daily_prices fa_anchor_dp "
            f"WHERE fa_anchor_dp.stock_id = {price_alias}.stock_id "
            f"AND fa_anchor_dp.\"date\" <= {anchor}"
            f")"
        )

    normalized_sql = _LOOKBACK_PRICE_ANCHOR_RE.sub(replace, screening_sql)
    normalized_sql, global_notes = _normalize_global_price_date_anchors(normalized_sql, universe)
    return normalized_sql, notes + global_notes


def _supabase_post(table: str, body: dict) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_benchmark_return(runner: OracleSQLRunner, symbol: str, start_date: str, end_date: str) -> float:
    sql = f"""
        select
            (select close from (
                select close, row_number() over (order by "date" desc) rn
                from benchmark_daily_prices
                where symbol = '{symbol}'
                  and "date" <= TO_DATE('{end_date}','YYYY-MM-DD')
            ) where rn = 1)
            /
            nullif((select close from (
                select close, row_number() over (order by "date" asc) rn
                from benchmark_daily_prices
                where symbol = '{symbol}'
                  and "date" >= TO_DATE('{start_date}','YYYY-MM-DD')
            ) where rn = 1), 0)
            - 1 as bench_return
        from dual
    """
    try:
        _, rows = runner(sql)
        if rows and rows[0].get("bench_return") is not None:
            return float(rows[0]["bench_return"])
    except Exception:
        pass
    return 0.0


def _get_period_return(runner: OracleSQLRunner, stock_ids: list[int], start_date: str, end_date: str) -> dict:
    if not stock_ids:
        return {"return": 0.0, "count": 0}

    id_list = ",".join(str(sid) for sid in stock_ids)
    sql = f"""
        with entry_prices as (
            select stock_id, close as price from (
                select dp.stock_id, dp.close, row_number() over (
                    partition by dp.stock_id order by dp."date" asc
                ) rn from daily_prices dp
                where dp.stock_id in ({id_list})
                  and dp."date" >= TO_DATE('{start_date}','YYYY-MM-DD')
            ) where rn = 1
        ),
        exit_prices as (
            select stock_id, close as price from (
                select dp.stock_id, dp.close, row_number() over (
                    partition by dp.stock_id order by dp."date" desc
                ) rn from daily_prices dp
                where dp.stock_id in ({id_list})
                  and dp."date" <= TO_DATE('{end_date}','YYYY-MM-DD')
            ) where rn = 1
        )
        select
            count(*) as cnt,
            avg(case when ep.price > 0 then (xp.price - ep.price) / ep.price else 0 end) as avg_return
        from entry_prices ep
        join exit_prices xp on xp.stock_id = ep.stock_id
        where ep.price > 0
    """
    try:
        _, rows = runner(sql)
        if rows:
            return {
                "return": float(rows[0].get("avg_return", 0) or 0),
                "count": int(rows[0].get("cnt", 0) or 0),
            }
    except Exception:
        pass
    return {"return": 0.0, "count": 0}


def _build_rebal_dates(universe: str, years: int, rebalance: str, now: datetime, *, months: int = 0) -> list[tuple[str, str]]:
    """Return list of (start_date, end_date) tuples for each rebalancing period."""
    if months > 0:
        # Calculate start from months instead of years
        start_m = now.month - months
        start_year = now.year + (start_m - 1) // 12  # handles negative months
        start_month = ((start_m - 1) % 12) + 1
    else:
        start_year = now.year - years
        start_month = 1
    rebal_dates: list[tuple[str, str]] = []

    if rebalance == "monthly":
        y, m = start_year, start_month
        while (y, m) <= (now.year, now.month):
            nm = (m % 12) + 1
            ny = y + (1 if m == 12 else 0)
            rebal_dates.append((f"{y}-{m:02d}-01", f"{ny}-{nm:02d}-01"))
            m, y = nm, ny

    else:
        schedule = _rebalance_schedule(universe)
        months = [s[0] for s in schedule]

        if rebalance == "semiannual":
            months = [months[0], months[2]]
        elif rebalance == "annual":
            months = [months[0]]

        points = [
            f"{y}-{m:02d}-01"
            for y in range(start_year, now.year + 1)
            for m in months
            if (y, m) >= (start_year, start_month) and (y, m) <= (now.year, now.month)
        ]

        for i in range(len(points) - 1):
            rebal_dates.append((points[i], points[i + 1]))

    return rebal_dates


def _run_backtest_logic(
    runner: OracleSQLRunner,
    screening_sql: str,
    universe: str,
    years: int,
    rebalance: str,
    *,
    months: int = 0,
) -> dict:
    now = datetime.now(timezone.utc)
    rebal_dates = _build_rebal_dates(universe, years, rebalance, now, months=months)

    if len(rebal_dates) < 2:
        return {"error": "백테스트 기간이 너무 짧습니다."}

    equity_curve = []
    period_returns = []
    portfolio_value = 10000.0
    benchmark_value = 10000.0
    peak = portfolio_value
    max_dd = 0.0
    total_holdings = 0
    period_count = 0

    periods_per_year = {
        "monthly": 12,
        "quarterly": len(_rebalance_schedule(universe)),
        "semiannual": 2,
        "annual": 1,
    }.get(rebalance, 4)

    bench_symbol = _benchmark_symbol(universe)

    for start_date, end_date in rebal_dates[:-1]:
        period_label = start_date[:7]

        # Substitute as_of_date into screening SQL
        try:
            sql = screening_sql.format(as_of_date=start_date)
        except KeyError as e:
            return {"error": f"screening_sql에 알 수 없는 플레이스홀더가 있습니다: {e}"}

        try:
            _, rows = runner(sql)
            stock_ids = [int(r["stock_id"]) for r in rows if r.get("stock_id")]
        except Exception as exc:
            stock_ids = []

        if not stock_ids:
            period_returns.append({"period": period_label, "return_pct": 0.0, "benchmark_pct": 0.0, "holdings": 0})
            equity_curve.append({"date": start_date, "portfolio": round(portfolio_value, 2), "benchmark": round(benchmark_value, 2)})
            continue

        result = _get_period_return(runner, stock_ids, start_date, end_date)
        period_ret = result["return"] - 0.003  # 0.3% transaction cost

        bench_ret = _get_benchmark_return(runner, bench_symbol, start_date, end_date)

        portfolio_value *= (1 + period_ret)
        benchmark_value *= (1 + bench_ret)

        if portfolio_value > peak:
            peak = portfolio_value
        dd = (portfolio_value - peak) / peak
        if dd < max_dd:
            max_dd = dd

        total_holdings += len(stock_ids)
        period_count += 1

        period_returns.append({
            "period": period_label,
            "return_pct": round(period_ret * 100, 2),
            "benchmark_pct": round(bench_ret * 100, 2),
            "holdings": len(stock_ids),
        })
        equity_curve.append({
            "date": start_date,
            "portfolio": round(portfolio_value, 2),
            "benchmark": round(benchmark_value, 2),
        })

    # Trim leading empty periods (no holdings = no data for that range)
    first_active = next((i for i, pr in enumerate(period_returns) if pr["holdings"] > 0), len(period_returns))
    if first_active > 0:
        period_returns = period_returns[first_active:]
        equity_curve = equity_curve[first_active:]
        # Recalculate from trimmed start
        if period_returns:
            portfolio_value = 10000.0
            benchmark_value = 10000.0
            peak = portfolio_value
            max_dd = 0.0
            for i, pr in enumerate(period_returns):
                portfolio_value *= (1 + pr["return_pct"] / 100)
                benchmark_value *= (1 + pr["benchmark_pct"] / 100)
                if portfolio_value > peak:
                    peak = portfolio_value
                dd = (portfolio_value - peak) / peak
                if dd < max_dd:
                    max_dd = dd
                if i < len(equity_curve):
                    equity_curve[i]["portfolio"] = round(portfolio_value, 2)
                    equity_curve[i]["benchmark"] = round(benchmark_value, 2)

    total_return = (portfolio_value / 10000.0) - 1
    bench_total = (benchmark_value / 10000.0) - 1
    actual_years = max(len(period_returns), 1) / periods_per_year

    cagr = (math.pow(1 + total_return, 1 / actual_years) - 1) * 100 if actual_years > 0 and total_return > -1 else 0
    bench_cagr = (math.pow(1 + bench_total, 1 / actual_years) - 1) * 100 if actual_years > 0 and bench_total > -1 else 0

    if period_returns:
        rets = [p["return_pct"] / 100 for p in period_returns]
        mean_ret = sum(rets) / len(rets)
        var_ret = sum((r - mean_ret) ** 2 for r in rets) / max(len(rets) - 1, 1)
        annual_vol = math.sqrt(var_ret * periods_per_year)
        sharpe = (cagr / 100) / annual_vol if annual_vol > 0 else 0
    else:
        sharpe = 0

    return {
        "cagr_pct": round(cagr, 2),
        "mdd_pct": round(max_dd * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "total_return_pct": round(total_return * 100, 2),
        "benchmark_cagr_pct": round(bench_cagr, 2),
        "excess_return_pct": round(cagr - bench_cagr, 2),
        "avg_holding_count": round(
            sum(pr["holdings"] for pr in period_returns) / max(len([pr for pr in period_returns if pr["holdings"] > 0]), 1), 1
        ),
        "equity_curve": equity_curve,
        "period_returns": period_returns,
    }


@dataclass(slots=True)
class RunBacktestAction(Action):
    strategy_name: str = ""
    screening_sql: str = ""
    universe: str = "KOSPI"
    years: int = 5
    rebalance: str = "quarterly"
    months: int = 0
    method_summary: str | None = None
    assumptions: str | None = None
    caveats: str | None = None

    def to_arguments_json(self) -> str:
        d: dict[str, Any] = {
            "strategy_name": self.strategy_name,
            "screening_sql": self.screening_sql,
            "universe": self.universe,
            "years": self.years,
            "rebalance": self.rebalance,
        }
        if self.method_summary:
            d["method_summary"] = self.method_summary
        if self.assumptions:
            d["assumptions"] = self.assumptions
        if self.caveats:
            d["caveats"] = self.caveats
        if self.months > 0:
            d["months"] = self.months
        return json.dumps(d, ensure_ascii=False)


@dataclass(slots=True)
class RunBacktestObservation(Observation):
    success: bool = False
    summary: str = ""
    cagr_pct: float = 0.0
    mdd_pct: float = 0.0
    total_return_pct: float = 0.0
    excess_return_pct: float = 0.0
    period_count: int = 0
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_count: int = 0
    method_summary: str | None = None
    assumptions: str | None = None
    caveats: str | None = None

    def to_text(self) -> str:
        if self.success:
            text = (
                f"Backtest completed. CAGR: {self.cagr_pct}%, MDD: {self.mdd_pct}%, "
                f"Total return: {self.total_return_pct}%, Excess vs benchmark: {self.excess_return_pct}%p. "
                f"{self.summary}"
            )
            if self.method_summary:
                text += f" method_summary={self.method_summary}"
            if self.assumptions:
                text += f" assumptions={self.assumptions}"
            if self.caveats:
                text += f" caveats={self.caveats}"
            if self.rows and all(r.get("return_pct", 0) == 0 and r.get("holdings", 0) == 0 for r in self.rows):
                text += " WARNING: All periods returned 0 holdings — the screening SQL likely matched no stocks. Check the query."
            return text
        return f"Backtest failed: {self.summary}"


def _execute(action: RunBacktestAction, conversation: Any) -> RunBacktestObservation:
    start_time = time.time()
    state = conversation.state
    user_id = state.get_agent_state("user_id")

    if not action.screening_sql.strip():
        return RunBacktestObservation(success=False, summary="screening_sql이 비어 있습니다.")
    if not _explicitly_requests_backtest(conversation):
        return RunBacktestObservation(
            success=False,
            summary=(
                "Backtest skipped: the latest user turn did not explicitly request a backtest. "
                "Answer from the current screening/query context instead."
            ),
        )

    runner = OracleSQLRunner()
    screening_sql, normalization_notes = _normalize_screening_sql_date_anchors(action.screening_sql, action.universe)

    try:
        results = _run_backtest_logic(
            runner=runner,
            screening_sql=screening_sql,
            universe=action.universe,
            years=action.years,
            rebalance=action.rebalance,
            months=action.months,
        )
    except Exception as exc:
        return RunBacktestObservation(success=False, summary=str(exc))

    if "error" in results:
        return RunBacktestObservation(success=False, summary=results["error"])

    elapsed_ms = int((time.time() - start_time) * 1000)

    if user_id and SUPABASE_URL and SUPABASE_SERVICE_KEY:
        try:
            _supabase_post("backtest_results", {
                "user_id": user_id,
                "session_id": state.get_agent_state("session_id"),
                "strategy_name": action.strategy_name,
                "strategy_description": screening_sql,
                "conditions": [],
                "universe": action.universe,
                "rebalance_period": action.rebalance,
                "backtest_years": action.months / 12 if action.months > 0 else action.years,
                "cagr_pct": results["cagr_pct"],
                "mdd_pct": results["mdd_pct"],
                "sharpe_ratio": results["sharpe_ratio"],
                "total_return_pct": results["total_return_pct"],
                "benchmark_cagr_pct": results["benchmark_cagr_pct"],
                "excess_return_pct": results["excess_return_pct"],
                "avg_holding_count": results["avg_holding_count"],
                "equity_curve": results["equity_curve"],
                "period_returns": results["period_returns"],
                "result_summary": f"CAGR {results['cagr_pct']}%, MDD {results['mdd_pct']}%",
                "status": "completed",
                "elapsed_ms": elapsed_ms,
            })
        except Exception:
            pass

    period_label = f"{action.months}개월" if action.months > 0 else f"{action.years}년"
    summary = (
        f"{action.strategy_name}: {period_label}간 {action.universe} 대상, "
        f"{action.rebalance} 리밸런싱. "
        f"과거 수익률이 미래 수익률을 보장하지 않습니다."
    )
    if normalization_notes:
        summary += " 가격 기준일은 휴일/비거래일이면 직전 거래일 종가로 자동 보정했습니다."

    method_summary = action.method_summary
    if normalization_notes:
        normalization_summary = (
            "백테스트 도구가 과거 가격 기준일 앵커를 직전 거래일 기준으로 자동 정규화함 "
            f"({len(normalization_notes)}개 조건)."
        )
        method_summary = f"{method_summary.rstrip()}\n{normalization_summary}" if method_summary else normalization_summary

    period_rows = results.get("period_returns", [])
    eq_curve = results.get("equity_curve", [])

    display_rows = []
    for i, pr in enumerate(period_rows):
        row = {
            "period": pr["period"],
            "return_pct": pr["return_pct"],
            "benchmark_pct": pr["benchmark_pct"],
            "excess_pct": round(pr["return_pct"] - pr["benchmark_pct"], 2),
            "holdings": pr["holdings"],
        }
        if i < len(eq_curve):
            row["portfolio_value"] = eq_curve[i]["portfolio"]
            row["benchmark_value"] = eq_curve[i]["benchmark"]
        display_rows.append(row)

    display_columns = ["period", "return_pct", "benchmark_pct", "excess_pct", "holdings", "portfolio_value", "benchmark_value"]

    return RunBacktestObservation(
        success=True,
        summary=summary,
        cagr_pct=results["cagr_pct"],
        mdd_pct=results["mdd_pct"],
        total_return_pct=results["total_return_pct"],
        excess_return_pct=results["excess_return_pct"],
        period_count=len(period_rows),
        columns=display_columns,
        rows=display_rows,
        row_count=len(display_rows),
        method_summary=method_summary,
        assumptions=action.assumptions,
        caveats=action.caveats,
    )


@dataclass(slots=True)
class RunBacktestTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "Name for this strategy (e.g. '기관 순매수 상위 저PER 전략')",
                },
                "screening_sql": {
                    "type": "string",
                    "description": (
                        "Oracle SQL returning 'stock_id' column. Uses {as_of_date} placeholder "
                        "(engine substitutes YYYY-MM-DD per period). "
                        "ONLY use criteria the user mentioned — NEVER invent conditions. "
                        "Adapt from SQL already used in this session, replacing dates with TO_DATE('{as_of_date}','YYYY-MM-DD'). "
                        "For lookback price anchors such as m1_dt, m6_dt, or m12_dt, use the latest trading day <= anchor date, "
                        "not the first trading day after the anchor. The tool auto-normalizes obvious "
                        "price.\"date\" BETWEEN m12_dt AND ref_dt anchor mistakes to same-stock MAX(date <= m12_dt). "
                        "When deriving global price anchor dates from daily_prices, scope MAX(\"date\") to the requested universe; "
                        "the tool auto-normalizes unscoped MAX(\"date\") daily_prices anchors."
                    ),
                },
                "universe": {
                    "type": "string",
                    "enum": ["KOSPI", "KOSDAQ", "SP500", "NASDAQ", "ALL"],
                    "description": "Stock universe for benchmark selection. KR→KS11, US→SPY.",
                },
                "years": {
                    "type": "integer",
                    "description": "Backtest period in years (default: 5, max: 10). Ignored when months is set.",
                },
                "months": {
                    "type": "integer",
                    "description": "Backtest period in months. Use this for sub-year periods (e.g. 3 for 3개월). When set, years is ignored. Use rebalance='monthly' with short periods.",
                },
                "rebalance": {
                    "type": "string",
                    "enum": ["monthly", "quarterly", "semiannual", "annual"],
                    "description": (
                        "Rebalancing frequency. "
                        "monthly: for flow/sentiment conditions (수급, 공매도 등). "
                        "quarterly: for financial statement conditions (PER, ROE 등). "
                        "Mixed (flow + financial): use quarterly."
                    ),
                },
                "method_summary": {
                    "type": "string",
                    "description": (
                        "Human-readable implementation summary for the backtest. "
                        "State universe, rebalance schedule, screening criteria, date/lag handling, ranking, and portfolio construction. "
                        "Do not paste SQL."
                    ),
                },
                "assumptions": {
                    "type": "string",
                    "description": "User-visible assumptions/defaults used by this backtest, or '없음' when none are material.",
                },
                "caveats": {
                    "type": "string",
                    "description": (
                        "User-visible caveats such as omitted filters, approximate data handling, hardcoded periods, "
                        "or limitations of screening_sql. Use '없음' only when none are material."
                    ),
                },
            },
            "required": ["strategy_name", "screening_sql", "method_summary", "assumptions", "caveats"],
        }


def make_run_backtest_tool() -> RunBacktestTool:
    return RunBacktestTool(
        name="run_backtest",
        description=(
            "Run historical backtest with {as_of_date} placeholder in screening SQL. "
            "Only call when user explicitly requests a backtest — for condition/result questions, answer from session context. "
            "Lookback price anchors are invariant: if an anchor date is a holiday/non-trading day, use the previous trading day. "
            "The tool automatically normalizes obvious BETWEEN-anchor mistakes for m1/m6/m9/m12/m24/m36 lookback price anchors. "
            "The tool also normalizes unscoped daily_prices MAX(date) anchors to the requested universe trading calendar. "
            "If some conditions are excluded (e.g. insufficient data), state what was included/excluded and why. "
            "Always fill method_summary, assumptions, and caveats so the final answer can show what was actually implemented. "
            "Always include: 과거 수익률이 미래 수익률을 보장하지 않습니다."
        ),
        action_type=RunBacktestAction,
        observation_type=RunBacktestObservation,
        executor=_execute,
    )
