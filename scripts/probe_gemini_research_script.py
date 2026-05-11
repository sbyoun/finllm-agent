#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runtime.env import load_env, require_env
from agent_runtime.llm.gemini import GeminiClient


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results" / "gemini_research_script_probe"

SYSTEM_PROMPT = """You are a senior quantitative research engineer.

Write production-grade, auditable Python research code. Prefer explicit data
lineage, point-in-time safety, intermediate validation, and reproducible logs
over clever shortcuts. If a factor definition is ambiguous, encode it as a
named function and document the exact convention you used.
"""

USER_PROMPT = """We are evaluating whether you can author a Python research script for a financial backtest.

Task:
Write a complete Python script that researches and backtests a Korean equity strategy.
The script will run inside a backend research sandbox, not inside a notebook.
Do not give a high-level explanation only; provide runnable Python code and brief notes.

Available database tables:
- stocks: id, ticker, name, country, market, instrument_type
- daily_prices: stock_id, "date", open, high, low, close, volume
- financial_statements: stock_id, account_id, year, quarter, accounting_date, value
- benchmark_daily_prices: symbol, "date", close

KR financial account IDs:
- 6592: revenue / sale_account
- 6594: gross profit / sale_totl_prfi
- 6597: operating income / bsop_prti
- 6606: total assets

Strategy spec:
- Universe: KR KOSPI common stocks only, exclude ETFs.
- Rebalance: monthly.
- Holdings: top 3 stocks, equal weight.
- Price signals for a rebalance date must exclude the current month.
- For as_of_date = 2026-05-01, use 2026-04 as the signal month.
- Financial statement availability must follow Korean disclosure windows:
  Q1 is available from Jun 1, Q2 from Sep 1, Q3 from Dec 1, Q4 from Apr 1 of the next year.
- Use only financial statements available as of the rebalance date.
- Factors, all higher-is-better:
  return_12m
  return_12m_ex_1m
  return_6m
  GP_A = gross profit / total assets
  Revenue_YoY
  Operating Income_YoY
  log_avg_turnover_3m
- Hard filter:
  volatility_3m <= 0.85
- Intended volatility_3m convention:
  trailing 63 trading-day standard deviation of daily returns, annualized by sqrt(252).
- Intended log_avg_turnover_3m convention:
  log1p of trailing 63 trading-day average value traded, where value traded = close * volume.
- return_12m convention:
  use 252 trading-day lag and reject rows when the actual calendar gap is outside 330 to 430 days.
- Ranking:
  rank each factor cross-sectionally within the signal month, higher values rank better.
  Combine by summing ranks or percentile ranks and select the best top 3.
- Include a function that can print the selected tickers and factor values for a single as_of_date.
- Include a monthly backtest runner and basic metrics.
- Include validation checks that make implementation mistakes visible.

Constraints:
- Do not assume a wide financial table.
- Do not use current-date shortcuts.
- Do not write to the production database.
- Do not call brokerage/trading APIs.
- Use read-only database access assumptions.
- Avoid leaking credentials in logs.
"""

STRICT_DB_APPENDIX = """

Additional strict requirements for this trial:
- Do not use mock data, random data, synthetic data, placeholders, or TODO-only data loaders.
- The script must include a real read-only database access layer using either:
  1) an injected SQL runner callable, or
  2) SQLAlchemy/oracledb/pandas.read_sql with credentials loaded from environment variables.
- The script must query the actual tables listed above.
- It is acceptable for the script to be non-executable without real credentials, but the data-access functions must contain concrete SQL.
- Include a dry-run entrypoint that prints the as_of_date selection for 2026-05-01.
- Do not include any expected output tickers.
"""


EXPECTED_TICKERS_FOR_REVIEW_ONLY = ["278470", "298040", "267260"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Gemini Flash research-script authoring quality.")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env.staging")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt-mode", choices=["base", "strict_db"], default="base")
    return parser.parse_args()


def extract_code_blocks(text: str) -> list[str]:
    blocks = []
    for match in re.finditer(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        blocks.append(match.group(1).strip())
    return blocks


def contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def evaluate_response(response: str) -> dict[str, Any]:
    code_blocks = extract_code_blocks(response)
    code = "\n\n".join(code_blocks)
    target = (code or response).lower()

    checks = {
        "uses_252_trading_day_lag": contains_any(target, ["252"]),
        "validates_330_430_day_gap": contains_any(target, ["330"]) and contains_any(target, ["430"]),
        "uses_63_trading_day_window": contains_any(target, ["63"]),
        "annualizes_volatility_sqrt_252": contains_any(target, ["sqrt(252", "sqrt(252)", "np.sqrt(252", "math.sqrt(252"]),
        "uses_log1p_turnover": contains_any(target, ["log1p", "log(1 +", "ln(1 +"]),
        "implements_kr_disclosure_windows": (
            contains_any(target, ["jun", "6, 1", "month=6", "6/1"])
            and contains_any(target, ["sep", "9, 1", "month=9", "9/1"])
            and contains_any(target, ["dec", "12, 1", "month=12", "12/1"])
            and contains_any(target, ["apr", "4, 1", "month=4", "4/1"])
        ),
        "excludes_current_month": contains_any(target, ["current month", "signal month", "previous month", "exclude"]),
        "uses_long_financial_statements": contains_any(target, ["financial_statements"]) and contains_any(target, ["account_id"]),
        "filters_kospi_common_stock": contains_any(target, ["kospi"]) and contains_any(target, ["instrument_type"]),
        "implements_volatility_filter_085": contains_any(target, ["0.85"]),
        "implements_cross_sectional_ranking": contains_any(target, ["rank"]) and contains_any(target, ["cross"]),
        "includes_single_date_selection_function": contains_any(target, ["as_of_date"]) and contains_any(target, ["selected", "select"]),
        "includes_backtest_runner": contains_any(target, ["backtest"]) and contains_any(target, ["monthly"]),
        "includes_validation_checks": contains_any(target, ["validate", "assert", "check"]),
        "uses_real_db_access": contains_any(target, ["read_sql", "oracledb", "sqlalchemy", "create_engine", "execute("]),
        "contains_concrete_sql": contains_any(target, ["select"]) and contains_any(target, ["from stocks"]) and contains_any(target, ["daily_prices"]),
        "avoids_trading_api": not contains_any(target, ["broker", "place_order", "kis order", "buy_order", "sell_order"]),
    }

    hard_flags = {
        "mentions_hidden_expected_tickers": any(ticker in response for ticker in EXPECTED_TICKERS_FOR_REVIEW_ONLY),
        "suspicious_volatility_times_100": bool(re.search(r"(volatility|vol_3m|stddev|std)\w*[^\\n]{0,80}\*\s*100", target)),
        "uses_sysdate_or_today_shortcut": contains_any(target, ["sysdate", "current_date", "date.today()", "datetime.now()"]),
        "assumes_wide_financial_table": contains_any(target, ["pivot"]) and not contains_any(target, ["account_id"]),
        "uses_mock_or_synthetic_data": contains_any(target, ["mock", "synthetic", "random", "np.random", "placeholder", "todo"]),
    }

    passed = sum(1 for value in checks.values() if value)
    failed = [name for name, value in checks.items() if not value]
    flags = [name for name, value in hard_flags.items() if value]

    return {
        "score": passed,
        "max_score": len(checks),
        "checks": checks,
        "failed_checks": failed,
        "hard_flags": flags,
        "code_block_count": len(code_blocks),
        "first_code_chars": len(code_blocks[0]) if code_blocks else 0,
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Gemini Research Script Probe",
        "",
        f"- started_at: `{summary['started_at']}`",
        f"- model: `{summary['model']}`",
        f"- prompt_mode: `{summary['prompt_mode']}`",
        f"- trials: `{summary['trials']}`",
        f"- expected top3 for reviewer only: `{', '.join(EXPECTED_TICKERS_FOR_REVIEW_ONLY)}`",
        "",
        "| trial | status | elapsed_ms | score | hard_flags | failed_checks | response | code |",
        "|---:|---|---:|---:|---|---|---|---|",
    ]
    for item in summary["results"]:
        eval_result = item.get("evaluation") or {}
        failed = ", ".join(eval_result.get("failed_checks") or [])
        flags = ", ".join(eval_result.get("hard_flags") or [])
        lines.append(
            "| {trial} | {status} | {elapsed_ms} | {score}/{max_score} | {flags} | {failed} | {response} | {code} |".format(
                trial=item["trial"],
                status=item["status"],
                elapsed_ms=item.get("elapsed_ms", 0),
                score=eval_result.get("score", 0),
                max_score=eval_result.get("max_score", 0),
                flags=flags or "-",
                failed=failed or "-",
                response=f"[response](trial_{item['trial']:02d}_response.md)",
                code=f"[code](trial_{item['trial']:02d}_code.py)" if item.get("code_written") else "-",
            )
        )
    lines.append("")
    lines.append("## Prompt")
    lines.append("")
    lines.append("See [prompt.md](prompt.md).")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- The expected top3 tickers are not included in the Gemini prompt.")
    lines.append("- This probe does not execute generated code; it evaluates response structure and key implementation details.")
    lines.append("- Manual review should focus on generated scripts, not only the numeric rubric.")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    load_env(args.env)

    model = args.model.strip() or os.getenv("MANAGED_GEMINI_MODEL", "").strip() or require_env("MANAGED_GEMINI_MODEL")
    api_key = require_env("MANAGED_GEMINI_API_KEY")
    client = GeminiClient(model=model, api_key=api_key)
    selected_user_prompt = USER_PROMPT + (STRICT_DB_APPENDIX if args.prompt_mode == "strict_db" else "")

    started_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.results_root / f"{started_at}_{args.prompt_mode}"
    run_dir.mkdir(parents=True, exist_ok=False)

    prompt_payload = {
        "system": SYSTEM_PROMPT,
        "user": selected_user_prompt,
        "hidden_expected_tickers_for_review_only": EXPECTED_TICKERS_FOR_REVIEW_ONLY,
    }
    write_json(run_dir / "prompt.json", prompt_payload)
    (run_dir / "prompt.md").write_text(
        "# System Prompt\n\n" + SYSTEM_PROMPT + "\n\n# User Prompt\n\n" + selected_user_prompt + "\n",
        encoding="utf-8",
    )

    results: list[dict[str, Any]] = []
    for trial in range(1, args.trials + 1):
        trial_user_prompt = (
            selected_user_prompt
            + f"\n\nTrial metadata: this is independent trial {trial}. Do not refer to any previous trial.\n"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": trial_user_prompt},
        ]

        started = time.time()
        item: dict[str, Any] = {"trial": trial, "status": "unknown"}
        try:
            response = client.completion(messages).message.content
            elapsed_ms = int((time.time() - started) * 1000)
            evaluation = evaluate_response(response)

            response_path = run_dir / f"trial_{trial:02d}_response.md"
            response_path.write_text(response + "\n", encoding="utf-8")

            code_blocks = extract_code_blocks(response)
            code_written = False
            if code_blocks:
                (run_dir / f"trial_{trial:02d}_code.py").write_text(code_blocks[0] + "\n", encoding="utf-8")
                code_written = True

            item.update(
                {
                    "status": "ok",
                    "elapsed_ms": elapsed_ms,
                    "evaluation": evaluation,
                    "response_chars": len(response),
                    "code_written": code_written,
                }
            )
            write_json(run_dir / f"trial_{trial:02d}_eval.json", item)
            print(
                f"trial={trial} status=ok elapsed_ms={elapsed_ms} "
                f"score={evaluation['score']}/{evaluation['max_score']} "
                f"flags={','.join(evaluation['hard_flags']) or '-'}",
                flush=True,
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - started) * 1000)
            item.update({"status": "error", "elapsed_ms": elapsed_ms, "error": str(exc)})
            write_json(run_dir / f"trial_{trial:02d}_eval.json", item)
            print(f"trial={trial} status=error elapsed_ms={elapsed_ms} error={exc}", flush=True)

        results.append(item)

    summary = {
        "started_at": started_at,
        "model": model,
        "prompt_mode": args.prompt_mode,
        "trials": args.trials,
        "run_dir": str(run_dir),
        "results": results,
    }
    write_json(run_dir / "summary.json", summary)
    write_summary(run_dir, summary)
    print(f"summary={run_dir / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
