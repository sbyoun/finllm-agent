"""run_backtest tool: backtest a factor-based strategy using historical data."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition
from agent_runtime.tool.sql.oracle import OracleSQLRunner


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Rebalancing quarter start months
QUARTER_MONTHS = [(1, 1), (4, 1), (7, 1), (10, 1)]


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


def _build_screening_sql(conditions: list[dict], universe: str, ref_year: int, ref_quarter: int) -> str:
    """Build SQL to screen stocks matching conditions at a given quarter."""
    metric_sql_map = {
        "per": """
            (select lc.close / nullif(le.value, 0)
             from (select stock_id, close from (
                select dp.stock_id, dp.close, row_number() over (
                    partition by dp.stock_id order by dp."date" desc
                ) rn from daily_prices dp
                where dp."date" <= TO_DATE('{y}-{m:02d}-01','YYYY-MM-DD')
             ) where rn = 1) lc
             join (select stock_id, value from (
                select fs.stock_id, fs.value, row_number() over (
                    partition by fs.stock_id order by fs.year desc, fs.quarter desc
                ) rn from financial_statements fs
                where fs.account_id = 6580 and (fs.year < {y} or (fs.year = {y} and fs.quarter <= {q}))
             ) where rn = 1) le on le.stock_id = lc.stock_id
             where lc.stock_id = s.id)""",
        "pbr": """
            (select lc.close / nullif(lb.value, 0)
             from (select stock_id, close from (
                select dp.stock_id, dp.close, row_number() over (
                    partition by dp.stock_id order by dp."date" desc
                ) rn from daily_prices dp
                where dp."date" <= TO_DATE('{y}-{m:02d}-01','YYYY-MM-DD')
             ) where rn = 1) lc
             join (select stock_id, value from (
                select fs.stock_id, fs.value, row_number() over (
                    partition by fs.stock_id order by fs.year desc, fs.quarter desc
                ) rn from financial_statements fs
                where fs.account_id = 6582 and (fs.year < {y} or (fs.year = {y} and fs.quarter <= {q}))
             ) where rn = 1) lb on lb.stock_id = lc.stock_id
             where lc.stock_id = s.id)""",
        "roe": """
            (select value from (
                select fs.stock_id, fs.value, row_number() over (
                    partition by fs.stock_id order by fs.year desc, fs.quarter desc
                ) rn from financial_statements fs
                where fs.account_id = 6579 and fs.stock_id = s.id
                  and (fs.year < {y} or (fs.year = {y} and fs.quarter <= {q}))
            ) where rn = 1)""",
        "operating_income_yoy": """
            (select case when prev.value > 0 then ((curr.value - prev.value) / prev.value) * 100 else null end
             from (select value from (
                select fs.value, row_number() over (order by fs.year desc, fs.quarter desc) rn
                from financial_statements fs
                where fs.account_id = 6597 and fs.stock_id = s.id
                  and (fs.year < {y} or (fs.year = {y} and fs.quarter <= {q}))
             ) where rn = 1) curr,
             (select value from (
                select fs.value, row_number() over (order by fs.year desc, fs.quarter desc) rn
                from financial_statements fs
                where fs.account_id = 6597 and fs.stock_id = s.id
                  and (fs.year < {y} - 1 or (fs.year = {y} - 1 and fs.quarter <= {q}))
             ) where rn = 1) prev)""",
        "revenue_yoy": """
            (select case when prev.value > 0 then ((curr.value - prev.value) / prev.value) * 100 else null end
             from (select value from (
                select fs.value, row_number() over (order by fs.year desc, fs.quarter desc) rn
                from financial_statements fs
                where fs.account_id = 6592 and fs.stock_id = s.id
                  and (fs.year < {y} or (fs.year = {y} and fs.quarter <= {q}))
             ) where rn = 1) curr,
             (select value from (
                select fs.value, row_number() over (order by fs.year desc, fs.quarter desc) rn
                from financial_statements fs
                where fs.account_id = 6592 and fs.stock_id = s.id
                  and (fs.year < {y} - 1 or (fs.year = {y} - 1 and fs.quarter <= {q}))
             ) where rn = 1) prev)""",
    }

    month = [1, 4, 7, 10][ref_quarter - 1] if 1 <= ref_quarter <= 4 else 1
    where_clauses = []

    for cond in conditions:
        metric = cond.get("metric", "").lower().replace(" ", "_")
        op = cond.get("operator", ">")
        val = cond.get("value", 0)

        if metric not in metric_sql_map:
            continue

        subquery = metric_sql_map[metric].format(y=ref_year, q=ref_quarter, m=month)
        where_clauses.append(f"{subquery} {op} {val}")

    if not where_clauses:
        return ""

    market_filter = f"s.market = '{universe}'" if universe != "ALL" else "1=1"

    return f"""
        select s.id as stock_id, s.ticker, s.name
        from stocks s
        where {market_filter}
          and s.country = 'KR'
          and {' and '.join(where_clauses)}
    """


def _get_period_return(runner: OracleSQLRunner, stock_ids: list[int], start_date: str, end_date: str) -> dict:
    """Get average equal-weight return for a basket of stocks over a period."""
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
        cols, rows = runner(sql)
        if rows:
            return {
                "return": float(rows[0].get("avg_return", 0) or 0),
                "count": int(rows[0].get("cnt", 0) or 0),
            }
    except Exception:
        pass
    return {"return": 0.0, "count": 0}


def _run_backtest_logic(
    runner: OracleSQLRunner,
    conditions: list[dict],
    universe: str,
    years: int,
    rebalance: str,
) -> dict:
    """Run the backtest and return results."""
    now = datetime.now(timezone.utc)
    start_year = now.year - years

    # Generate rebalancing dates
    if rebalance == "annual":
        periods = [(y, 1) for y in range(start_year, now.year + 1)]
    elif rebalance == "semiannual":
        periods = []
        for y in range(start_year, now.year + 1):
            periods.extend([(y, 1), (y, 3)])
    else:  # quarterly
        periods = []
        for y in range(start_year, now.year + 1):
            periods.extend([(y, q) for q in range(1, 5)])

    # Trim future periods
    current_q = (now.month - 1) // 3 + 1
    periods = [(y, q) for y, q in periods if y < now.year or (y == now.year and q <= current_q)]

    if len(periods) < 2:
        return {"error": "백테스트 기간이 너무 짧습니다."}

    equity_curve = []
    period_returns = []
    portfolio_value = 10000.0
    benchmark_value = 10000.0
    peak = portfolio_value
    max_dd = 0.0
    total_holdings = 0
    period_count = 0

    for i in range(len(periods) - 1):
        y, q = periods[i]
        ny, nq = periods[i + 1]

        month = [1, 4, 7, 10][q - 1]
        next_month = [1, 4, 7, 10][nq - 1]
        start_date = f"{y}-{month:02d}-01"
        end_date = f"{ny}-{next_month:02d}-01"

        # Screen stocks
        screening_sql = _build_screening_sql(conditions, universe, y, q)
        if not screening_sql:
            continue

        try:
            cols, rows = runner(screening_sql)
            stock_ids = [int(r["stock_id"]) for r in rows if r.get("stock_id")]
        except Exception:
            stock_ids = []

        if not stock_ids:
            period_returns.append({
                "period": f"{y}Q{q}",
                "return_pct": 0.0,
                "benchmark_pct": 0.0,
                "holdings": 0,
            })
            equity_curve.append({
                "date": start_date,
                "portfolio": round(portfolio_value, 2),
                "benchmark": round(benchmark_value, 2),
            })
            continue

        # Get portfolio return
        result = _get_period_return(runner, stock_ids, start_date, end_date)
        period_ret = result["return"]

        # Adjust for transaction cost (0.3% round trip)
        period_ret -= 0.003

        # Get benchmark return (all KOSPI stocks equal weight)
        bench_sql = f"""
            select s.id as stock_id from stocks s
            where s.market = '{universe}' and s.country = 'KR'
            fetch first 200 rows only
        """
        try:
            _, bench_rows = runner(bench_sql)
            bench_ids = [int(r["stock_id"]) for r in bench_rows if r.get("stock_id")]
            bench_result = _get_period_return(runner, bench_ids, start_date, end_date)
            bench_ret = bench_result["return"]
        except Exception:
            bench_ret = 0.0

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
            "period": f"{y}Q{q}",
            "return_pct": round(period_ret * 100, 2),
            "benchmark_pct": round(bench_ret * 100, 2),
            "holdings": len(stock_ids),
        })
        equity_curve.append({
            "date": start_date,
            "portfolio": round(portfolio_value, 2),
            "benchmark": round(benchmark_value, 2),
        })

    # Final metrics
    total_return = (portfolio_value / 10000.0) - 1
    bench_total = (benchmark_value / 10000.0) - 1
    actual_years = max(len(periods) - 1, 1) / 4  # quarters to years

    cagr = (math.pow(1 + total_return, 1 / actual_years) - 1) * 100 if actual_years > 0 and total_return > -1 else 0
    bench_cagr = (math.pow(1 + bench_total, 1 / actual_years) - 1) * 100 if actual_years > 0 and bench_total > -1 else 0

    # Simple Sharpe (annualized return / annualized vol)
    if period_returns:
        rets = [p["return_pct"] / 100 for p in period_returns]
        mean_ret = sum(rets) / len(rets)
        var_ret = sum((r - mean_ret) ** 2 for r in rets) / max(len(rets) - 1, 1)
        annual_vol = math.sqrt(var_ret * 4) if rebalance == "quarterly" else math.sqrt(var_ret * 2)  # annualize
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
    conditions: list = field(default_factory=list)
    universe: str = "KOSPI"
    years: int = 5
    rebalance: str = "quarterly"

    def to_arguments_json(self) -> str:
        return json.dumps({
            "strategy_name": self.strategy_name,
            "conditions": self.conditions,
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

    runner = OracleSQLRunner()

    try:
        results = _run_backtest_logic(
            runner=runner,
            conditions=action.conditions,
            universe=action.universe,
            years=action.years,
            rebalance=action.rebalance,
        )
    except Exception as exc:
        return RunBacktestObservation(success=False, summary=str(exc))

    if "error" in results:
        return RunBacktestObservation(success=False, summary=results["error"])

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Save to Supabase
    if user_id and SUPABASE_URL and SUPABASE_SERVICE_KEY:
        try:
            _supabase_post("backtest_results", {
                "user_id": user_id,
                "session_id": state.get_agent_state("session_id"),
                "strategy_name": action.strategy_name,
                "strategy_description": json.dumps(action.conditions, ensure_ascii=False),
                "conditions": action.conditions,
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
            pass  # Non-critical

    summary = (
        f"{action.strategy_name}: {action.years}년간 {action.universe} 대상, "
        f"{action.rebalance} 리밸런싱. "
        f"과거 수익률이 미래 수익률을 보장하지 않습니다."
    )

    return RunBacktestObservation(
        success=True,
        summary=summary,
        cagr_pct=results["cagr_pct"],
        mdd_pct=results["mdd_pct"],
        total_return_pct=results["total_return_pct"],
        excess_return_pct=results["excess_return_pct"],
        period_count=len(results.get("period_returns", [])),
    )


@dataclass(slots=True)
class RunBacktestTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "Name for this strategy (e.g., '저PER 고성장 전략')",
                },
                "conditions": {
                    "type": "array",
                    "description": "Screening conditions. Each item: {metric, operator, value}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "enum": ["per", "pbr", "roe", "operating_income_yoy", "revenue_yoy"],
                            },
                            "operator": {"type": "string", "enum": ["<", ">", "<=", ">="]},
                            "value": {"type": "number"},
                        },
                        "required": ["metric", "operator", "value"],
                    },
                },
                "universe": {
                    "type": "string",
                    "enum": ["KOSPI", "KOSDAQ", "ALL"],
                    "description": "Stock universe (default: KOSPI)",
                },
                "years": {
                    "type": "integer",
                    "description": "Backtest period in years (default: 5, max: 10)",
                },
                "rebalance": {
                    "type": "string",
                    "enum": ["quarterly", "semiannual", "annual"],
                    "description": "Rebalancing frequency (default: quarterly)",
                },
            },
            "required": ["strategy_name", "conditions"],
        }


def make_run_backtest_tool() -> RunBacktestTool:
    return RunBacktestTool(
        name="run_backtest",
        description=(
            "Run a historical backtest for a factor-based stock screening strategy. "
            "Tests how a portfolio of stocks matching the given conditions would have performed. "
            "Supported metrics: per, pbr, roe, operating_income_yoy, revenue_yoy. "
            "Results are saved to the user's backtest archive. "
            "Always include the disclaimer: past returns do not guarantee future results."
        ),
        action_type=RunBacktestAction,
        observation_type=RunBacktestObservation,
        executor=_execute,
    )
