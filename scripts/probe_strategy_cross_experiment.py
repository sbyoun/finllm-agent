#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
for path in (SRC_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from agent_runtime.env import load_env, require_env  # noqa: E402
from agent_runtime.llm.gemini import GeminiClient  # noqa: E402
from agent_runtime.service import RuntimeAgentRequest, run_agent_request_json  # noqa: E402
from probe_research_sandbox_e2e import (  # noqa: E402
    EXPECTED_TOP3,
    RESULTS_ROOT as SANDBOX_RESULTS_ROOT,
    ensure_fixtures,
    extract_code,
    run_in_sandbox,
    static_guard,
    tickers_from_output,
    timed_gemini_completion,
)


RESULTS_ROOT = REPO_ROOT / "results" / "strategy_cross_experiment"

SANDBOX_SYSTEM_PROMPT = """You are a senior quantitative research engineer writing a single-file Python research script.

Follow the requested interface exactly. Prefer correctness, point-in-time safety,
and explicit validation. Do not include mock data. Do not use network, database,
environment variables, or files outside the paths provided by CLI arguments.
"""

SANDBOX_AMBIGUOUS_PROMPT = """Write a complete single-file Python script.

The script will run in a restricted research sandbox with only these input CSVs:
- --stocks: columns id,ticker,name,country,market,instrument_type
- --daily-prices: columns stock_id,date,close,volume
- --financial-statements: columns stock_id,account_id,year,quarter,accounting_date,value
- --as-of-date: YYYY-MM-DD
- --output: path for a JSON output file

User request:
"KOSPI에서 주도주를 잡아서 월마다 리밸런싱하는 전략을 만들고 싶다.
적당한 팩터를 찾아서 2026-05-01 기준 후보 3개를 보여줘."

Important:
- This is intentionally under-specified. Do not ask follow-up questions in this trial.
- Pick a reasonable leader-stock strategy yourself, but make every assumption explicit in diagnostics.strategy_spec.
- Use only the provided CSVs.
- Do not hardcode output tickers.
- Return JSON with:
  {
    "as_of_date": "...",
    "selected": [{"rank": 1, "ticker": "...", "name": "...", "factors": {...}}],
    "diagnostics": {"strategy_spec": {...}, "assumptions": [...]}
  }

Security/interface constraints:
- Use only pandas/numpy/argparse/json/math/datetime/pathlib from Python.
- Do not import os, subprocess, socket, requests, urllib, sqlalchemy, or database drivers.
- Do not read or write any path except the CLI input paths and the CLI output path.
- Return code only in a Python fenced block.
"""

FOLDALPHA_DETAILED_PROMPT = """KOSPI 월간 리밸런싱 전략을 조회 검증해줘.

목표:
- 2026-05-01 기준 후보 3개만 보여줘.
- SQL이나 내부 쿼리는 답변에 노출하지 마.
- 결과에는 종목명, 티커, 각 팩터 값을 보여줘.

전략 명세:
- Universe: KR KOSPI common stocks only. country='KR', market='KOSPI', instrument_type='stock'.
- Rebalance: monthly.
- Current month is excluded. For as_of_date=2026-05-01, signal month is 2026-04.
- Price signals must use only daily prices strictly before the first day of the as_of month.
- Financial availability must follow Korean disclosure windows:
  Q1 available from Jun 1, Q2 from Sep 1, Q3 from Dec 1, Q4 from Apr 1 of the next year.
- Use only financial statements available as of the rebalance date.
- Factors, all higher-is-better:
  return_12m
  return_12m_ex_1m
  return_6m
  GP_A
  Revenue_YoY
  Operating Income_YoY
  log_avg_turnover_3m
- Hard filter: volatility_3m <= 0.85.
- return_12m convention:
  compute on daily rows using 252 trading-day lag and reject rows when the actual calendar gap is outside 330 to 430 days.
- return_12m_ex_1m:
  compute on monthly last-close rows as close_1m_ago / close_12m_ago - 1.
- return_6m:
  compute on monthly last-close rows as close / close_6m_ago - 1.
- volatility_3m:
  trailing 63 trading-day stddev of daily returns, annualized by sqrt(252); min_periods=30 is acceptable.
- log_avg_turnover_3m:
  log1p of trailing 63 trading-day average close*volume; min_periods=30 is acceptable.
- Financial factors:
  GP_A = gross profit account_id 6594 / total assets account_id 6606.
  Revenue_YoY = revenue 6592 current available quarter / same quarter previous year - 1.
  Operating Income_YoY = operating income 6597 current available quarter / same quarter previous year - 1.
  Clip Revenue_YoY and Operating Income_YoY to [-5, 10].
- Ranking:
  rank each factor cross-sectionally for the signal month; all factors higher-is-better.
  combine ranks and select top 3.
  handle missing values explicitly and do not let missing values become top-ranked.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-test vague vs detailed strategy requests.")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env.staging")
    parser.add_argument("--sandbox-runs", type=int, default=3)
    parser.add_argument("--foldalpha-runs", type=int, default=1)
    parser.add_argument("--skip-fixtures", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_sandbox_ambiguous(
    *,
    client: GeminiClient,
    fixtures: dict[str, Path],
    run_root: Path,
    run_index: int,
) -> dict[str, Any]:
    run_dir = run_root / f"sandbox_ambiguous_{run_index:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "system", "content": SANDBOX_SYSTEM_PROMPT},
        {"role": "user", "content": SANDBOX_AMBIGUOUS_PROMPT},
    ]
    try:
        response, generation_ms = timed_gemini_completion(client, messages)
    except Exception as exc:
        return {"run": run_index, "status": "generation_failed", "error": repr(exc)}
    code = extract_code(response)
    (run_dir / "response.md").write_text(response + "\n", encoding="utf-8")
    (run_dir / "candidate.py").write_text(code, encoding="utf-8")

    findings = static_guard(code)
    if findings:
        return {
            "run": run_index,
            "status": "static_blocked",
            "generation_ms": generation_ms,
            "findings": findings,
        }

    execution = run_in_sandbox(
        code=code,
        fixtures=fixtures,
        run_dir=run_dir,
        as_of_date="2026-05-01",
        nproc_limit=0,
    )
    got = tickers_from_output(execution.get("output"))
    return {
        "run": run_index,
        "status": "executed" if execution["returncode"] == 0 else "execution_failed",
        "generation_ms": generation_ms,
        "execution_ms": execution["elapsed_ms"],
        "got_top3": got,
        "canonical_expected_top3": EXPECTED_TOP3,
        "canonical_exact_match": got == EXPECTED_TOP3,
        "canonical_overlap": len(set(got) & set(EXPECTED_TOP3)),
        "execution": execution,
    }


def run_foldalpha_detailed(*, run_index: int) -> dict[str, Any]:
    started = time.time()
    try:
        result = run_agent_request_json(
            RuntimeAgentRequest(
                question=FOLDALPHA_DETAILED_PROMPT,
                user_id=f"cross-probe-user-{run_index}",
                session_id=f"cross-probe-foldalpha-detailed-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{run_index}",
                max_iterations=8,
            )
        )
    except Exception as exc:
        return {"run": run_index, "status": "failed", "error": repr(exc)}

    rows = []
    if result.get("dataset"):
        rows = result["dataset"].get("rows") or []
    tickers = [str(row.get("ticker")).zfill(6) for row in rows[:3] if isinstance(row, dict) and row.get("ticker") is not None]
    sql = result.get("sql")
    assistant_message = ((result.get("decision") or {}).get("assistantMessage") or "")
    dataset_description = ((result.get("dataset") or {}).get("description") or "")
    return {
        "run": run_index,
        "status": result.get("status"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "mode": (result.get("decision") or {}).get("mode"),
        "assistant_head": assistant_message[:1200],
        "got_top3": tickers,
        "canonical_expected_top3": EXPECTED_TOP3,
        "canonical_exact_match": tickers == EXPECTED_TOP3,
        "canonical_overlap": len(set(tickers) & set(EXPECTED_TOP3)),
        "has_execution_notes": "### 실제 실행 기준" in assistant_message,
        "dataset_has_execution_notes": "### 실제 실행 기준" in dataset_description,
        "sql": sql,
        "dataset_rows": rows[:10],
        "metrics": result.get("metrics"),
    }


def write_summary(run_root: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Strategy Cross Experiment",
        "",
        f"- started_at: `{summary['started_at']}`",
        f"- model: `{summary['model']}`",
        f"- canonical_top3_for_detailed_spec: `{', '.join(EXPECTED_TOP3)}`",
        "",
        "## Sandbox Ambiguous",
        "",
        "| run | status | gen_ms | exec_ms | top3 | overlap | exact |",
        "|---:|---|---:|---:|---|---:|---|",
    ]
    for item in summary["sandbox_ambiguous"]:
        lines.append(
            f"| {item['run']} | {item['status']} | {item.get('generation_ms', '-')} | {item.get('execution_ms', '-')} | "
            f"`{', '.join(item.get('got_top3') or []) or '-'}` | {item.get('canonical_overlap', '-')} | {item.get('canonical_exact_match', '-')} |"
        )
    lines.extend(
        [
            "",
            "## Foldalpha Detailed",
            "",
            "| run | status | mode | elapsed_ms | top3 | overlap | exact | issue_flags |",
            "|---:|---|---|---:|---|---:|---|---|",
        ]
    )
    for item in summary["foldalpha_detailed"]:
        lines.append(
            f"| {item['run']} | {item['status']} | {item.get('mode')} | {item.get('elapsed_ms', '-')} | "
            f"`{', '.join(item.get('got_top3') or []) or '-'}` | {item.get('canonical_overlap', '-')} | "
            f"{item.get('canonical_exact_match', '-')} | {', '.join(item.get('sql_issue_flags') or []) or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Prompts",
            "",
            "- [sandbox_ambiguous_prompt.md](sandbox_ambiguous_prompt.md)",
            "- [foldalpha_detailed_prompt.md](foldalpha_detailed_prompt.md)",
        ]
    )
    (run_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    load_env(args.env)
    model = require_env("MANAGED_GEMINI_MODEL")
    client = GeminiClient(model=model, api_key=require_env("MANAGED_GEMINI_API_KEY"))

    started_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = RESULTS_ROOT / started_at
    run_root.mkdir(parents=True, exist_ok=False)
    (run_root / "sandbox_ambiguous_prompt.md").write_text(SANDBOX_AMBIGUOUS_PROMPT + "\n", encoding="utf-8")
    (run_root / "foldalpha_detailed_prompt.md").write_text(FOLDALPHA_DETAILED_PROMPT + "\n", encoding="utf-8")

    fixtures_dir = SANDBOX_RESULTS_ROOT / "_fixtures_kr_leader_20260501"
    fixtures = (
        {
            "stocks": fixtures_dir / "stocks.csv",
            "daily_prices": fixtures_dir / "daily_prices.csv",
            "financial_statements": fixtures_dir / "financial_statements.csv",
        }
        if args.skip_fixtures
        else ensure_fixtures(fixtures_dir)
    )

    sandbox_ambiguous = []
    for index in range(1, args.sandbox_runs + 1):
        item = run_sandbox_ambiguous(client=client, fixtures=fixtures, run_root=run_root, run_index=index)
        sandbox_ambiguous.append(item)
        write_json(run_root / "sandbox_ambiguous.partial.json", sandbox_ambiguous)
        print(
            f"sandbox_ambiguous={index} status={item['status']} "
            f"top3={item.get('got_top3')} overlap={item.get('canonical_overlap')}",
            flush=True,
        )

    foldalpha_detailed = []
    for index in range(1, args.foldalpha_runs + 1):
        item = run_foldalpha_detailed(run_index=index)
        foldalpha_detailed.append(item)
        write_json(run_root / "foldalpha_detailed.partial.json", foldalpha_detailed)
        print(
            f"foldalpha_detailed={index} status={item['status']} mode={item.get('mode')} "
            f"top3={item.get('got_top3')} overlap={item.get('canonical_overlap')} flags={item.get('sql_issue_flags')}",
            flush=True,
        )

    summary = {
        "started_at": started_at,
        "model": model,
        "canonical_expected_top3": EXPECTED_TOP3,
        "sandbox_ambiguous": sandbox_ambiguous,
        "foldalpha_detailed": foldalpha_detailed,
    }
    write_json(run_root / "summary.json", summary)
    write_summary(run_root, summary)
    print(f"summary={run_root / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
