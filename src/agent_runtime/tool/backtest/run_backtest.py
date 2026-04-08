"""run_backtest tool: backtest a factor-based strategy using historical data.

The agent writes a screening SQL with an {as_of_date} placeholder.
The engine substitutes the rebalancing date for each period and calculates returns.
"""

from __future__ import annotations

import json
import math
import os
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


def _rebalance_schedule(universe: str) -> list[tuple[int]]:
    return _KR_REBALANCE_SCHEDULE if universe in _KR_UNIVERSES else _US_REBALANCE_SCHEDULE


def _benchmark_symbol(universe: str) -> str:
    return "KS11" if universe in _KR_UNIVERSES else "SPY"


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


def _build_rebal_dates(universe: str, years: int, rebalance: str, now: datetime) -> list[tuple[str, str]]:
    """Return list of (start_date, end_date) tuples for each rebalancing period."""
    start_year = now.year - years
    rebal_dates: list[tuple[str, str]] = []

    if rebalance == "monthly":
        y, m = start_year, 1
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
            if (y, m) <= (now.year, now.month)
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
) -> dict:
    now = datetime.now(timezone.utc)
    rebal_dates = _build_rebal_dates(universe, years, rebalance, now)

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

    total_return = (portfolio_value / 10000.0) - 1
    bench_total = (benchmark_value / 10000.0) - 1
    actual_years = max(len(rebal_dates) - 1, 1) / periods_per_year

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
        "avg_holding_count": round(total_holdings / max(period_count, 1), 1),
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

    def to_arguments_json(self) -> str:
        return json.dumps({
            "strategy_name": self.strategy_name,
            "screening_sql": self.screening_sql,
            "universe": self.universe,
            "years": self.years,
            "rebalance": self.rebalance,
        }, ensure_ascii=False)


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

    def to_text(self) -> str:
        if self.success:
            return (
                f"Backtest completed. CAGR: {self.cagr_pct}%, MDD: {self.mdd_pct}%, "
                f"Total return: {self.total_return_pct}%, Excess vs benchmark: {self.excess_return_pct}%p. "
                f"{self.summary}"
            )
        return f"Backtest failed: {self.summary}"


def _execute(action: RunBacktestAction, conversation: Any) -> RunBacktestObservation:
    start_time = time.time()
    state = conversation.state
    user_id = state.get_agent_state("user_id")

    if not action.screening_sql.strip():
        return RunBacktestObservation(success=False, summary="screening_sql이 비어 있습니다.")

    runner = OracleSQLRunner()

    try:
        results = _run_backtest_logic(
            runner=runner,
            screening_sql=action.screening_sql,
            universe=action.universe,
            years=action.years,
            rebalance=action.rebalance,
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
                "strategy_description": action.screening_sql,
                "conditions": [],
                "universe": action.universe,
                "rebalance_period": action.rebalance,
                "backtest_years": action.years,
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

    summary = (
        f"{action.strategy_name}: {action.years}년간 {action.universe} 대상, "
        f"{action.rebalance} 리밸런싱. "
        f"과거 수익률이 미래 수익률을 보장하지 않습니다."
    )

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
                        "Oracle SQL that returns a 'stock_id' column for each rebalancing period. "
                        "MUST use {as_of_date} as the date placeholder — the engine substitutes the "
                        "rebalancing date (YYYY-MM-DD) for each period. "
                        "Examples:\n"
                        "  Flow (monthly): "
                        "SELECT s.id AS stock_id FROM stocks s "
                        "JOIN kr_investor_trade_daily k ON k.stock_id = s.id "
                        "WHERE k.\"date\" > TO_DATE('{as_of_date}','YYYY-MM-DD') - 30 "
                        "AND k.\"date\" <= TO_DATE('{as_of_date}','YYYY-MM-DD') "
                        "AND s.market = 'KOSPI' "
                        "GROUP BY s.id HAVING SUM(k.institution_net_value) > 0 "
                        "ORDER BY SUM(k.institution_net_value) DESC FETCH FIRST 50 ROWS ONLY\n"
                        "  Financial (quarterly): "
                        "SELECT s.id AS stock_id FROM stocks s "
                        "JOIN (SELECT stock_id, value FROM (SELECT stock_id, value, "
                        "ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY year DESC, quarter DESC) rn "
                        "FROM financial_statements WHERE account_id = 6580 "
                        "AND TO_DATE(year||'-'||(quarter*3)||'-28','YYYY-MM-DD') "
                        "<= TO_DATE('{as_of_date}','YYYY-MM-DD')) WHERE rn=1) eps ON eps.stock_id = s.id "
                        "JOIN (SELECT stock_id, close FROM (SELECT stock_id, close, "
                        "ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY \"date\" DESC) rn "
                        "FROM daily_prices WHERE \"date\" <= TO_DATE('{as_of_date}','YYYY-MM-DD')) WHERE rn=1) "
                        "p ON p.stock_id = s.id "
                        "WHERE s.market = 'KOSPI' AND p.close / NULLIF(eps.value, 0) < 15\n"
                        "Use the same SQL style as run_sql queries but replace hardcoded dates / sysdate "
                        "with TO_DATE('{as_of_date}','YYYY-MM-DD'). "
                        "For flow metrics use monthly rebalancing; for financial metrics use quarterly."
                    ),
                },
                "universe": {
                    "type": "string",
                    "enum": ["KOSPI", "KOSDAQ", "SP500", "NASDAQ", "ALL"],
                    "description": "Stock universe for benchmark selection. KR→KS11, US→SPY.",
                },
                "years": {
                    "type": "integer",
                    "description": "Backtest period in years (default: 5, max: 10)",
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
            },
            "required": ["strategy_name", "screening_sql"],
        }


def make_run_backtest_tool() -> RunBacktestTool:
    return RunBacktestTool(
        name="run_backtest",
        description=(
            "Run a historical backtest for any stock screening strategy. "
            "The agent writes a screening SQL with {as_of_date} placeholder; "
            "the engine runs it for each rebalancing period and calculates portfolio returns. "
            "The SQL must return a 'stock_id' column. "
            "Use the same SQL patterns as run_sql queries — just replace hardcoded dates or sysdate "
            "with TO_DATE('{as_of_date}','YYYY-MM-DD'). "
            "When the user asks to backtest a strategy discussed in this session, "
            "adapt the screening SQL already used (or planned) for that strategy. "
            "Include ALL criteria the user mentioned — do not drop flow or financial conditions. "
            "Benchmark: KS11 for KR markets, SPY for US markets. "
            "Results are saved to the user's backtest archive. "
            "Always include the disclaimer: 과거 수익률이 미래 수익률을 보장하지 않습니다."
        ),
        action_type=RunBacktestAction,
        observation_type=RunBacktestObservation,
        executor=_execute,
    )
