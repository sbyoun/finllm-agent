#!/usr/bin/env python3
"""Agent quality test scenarios — calls staging server (port 8001).

Usage:
  python3 scripts/test_scenarios.py          # run all
  python3 scripts/test_scenarios.py 2        # run scenario 2 only
  python3 scripts/test_scenarios.py 1 3      # run scenarios 1 and 3
"""

import os
import sys
import time
from datetime import datetime

import requests

BASE_URL = "http://127.0.0.1:8001"
USER_ID = "b01daffa-2a9f-41ec-a090-6c2099eaa7e0"

_checks: list[tuple[str, bool, str]] = []
_timings: list[tuple[str, int]] = []  # (label, elapsed_ms)


def run_turn(question: str, history: list[dict], session_id: str, state_snapshot: dict | None = None) -> dict:
    body = {
        "question": question,
        "userId": USER_ID,
        "sessionId": session_id,
        "history": history,
        "stateSnapshot": state_snapshot or {},
    }
    resp = requests.post(f"{BASE_URL}/runs/sync", json=body, timeout=180)
    resp.raise_for_status()
    return resp.json()


def build_history(turns: list[dict]) -> list[dict]:
    history = []
    for t in turns:
        history.append({"role": "user", "content": t["question"]})
        msg = t["result"].get("decision", {}).get("assistantMessage", "")
        if msg:
            history.append({"role": "assistant", "content": msg})
    return history


def check_dataset_values(scenario: str, ds: dict) -> bool:
    """Check that dataset rows don't have excessive zero/null values."""
    rows = ds.get("rows", [])
    cols = [c.get("key", "") for c in ds.get("columns", [])]
    if not rows or not cols:
        return True
    total_cells = len(rows) * len(cols)
    zero_null = 0
    for row in rows:
        for col in cols:
            val = row.get(col)
            if val is None or val == "" or val == 0 or val == "0":
                zero_null += 1
    ratio = zero_null / total_cells if total_cells else 0
    return check(scenario, "dataset no excessive zeros", ratio < 0.5,
                 f"{zero_null}/{total_cells} cells empty/zero ({ratio:.0%})")


def check(scenario: str, name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    msg = f"    [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    _checks.append((f"S{scenario}:{name}", passed, detail))
    return passed


def print_turn(scenario: str, turn_num: int, question: str, result: dict):
    metrics = result.get("metrics", {})
    exec_log = result.get("executionLog", [])
    msg = result.get("decision", {}).get("assistantMessage", "")
    datasets = result.get("datasets", [])
    elapsed = result.get("elapsedMs", 0)

    label = f"S{scenario}:T{turn_num}"
    _timings.append((label, elapsed))

    print(f"\n  Turn {turn_num} | {elapsed/1000:.1f}s | loops={metrics.get('loopCount')} sql={metrics.get('sqlCallCount')} news={metrics.get('newsCallCount',0)} tools={metrics.get('toolCallCount')}")
    print(f"  Q: {question}")
    print(f"  Exec: {exec_log}")
    if datasets:
        for ds in datasets:
            rows = ds.get("rows", [])
            cols = [c.get("key", "") for c in ds.get("columns", [])]
            print(f"  Dataset: \"{ds.get('title','?')}\" — {len(rows)} rows, cols={cols[:6]}{'...' if len(cols)>6 else ''}")
            if rows:
                print(f"  Row[0]: {rows[0]}")
                if len(rows) > 1:
                    print(f"  Row[1]: {rows[1]}")
    else:
        print("  Dataset: (none)")
    print(f"  Answer: {msg[:200]}{'...' if len(msg)>200 else ''}")


# ============================================================
def scenario_1():
    """Portfolio + News — MALFORMED_FUNCTION_CALL fix"""
    print(f"\n{'='*70}\n  S1: Portfolio + News\n{'='*70}")
    S, sid = "1", "test-s1"

    r = run_turn("내 포트폴리오 종목들의 최근 뉴스 요약해줘", [], sid)
    print_turn(S, 1, "내 포트폴리오 종목들의 최근 뉴스 요약해줘", r)

    msg = r.get("decision", {}).get("assistantMessage", "")
    exec_log = r.get("executionLog", [])

    ok = True
    ok &= check(S, "non-empty answer", "답변을 완성하지 못했습니다" not in msg and len(msg.strip()) > 50)
    ok &= check(S, "portfolio called", "get_portfolio" in str(exec_log))
    ok &= check(S, "news called", "search_news" in str(exec_log))
    return ok


# ============================================================
def scenario_2():
    """Context continuity — 방산주 실적 → 급등 가능성"""
    print(f"\n{'='*70}\n  S2: Context continuity (방산주)\n{'='*70}")
    S, sid = "2", "test-s2"

    q1 = "방산주 중 최근 실적 개선 폭이 큰 종목 정리해줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    metrics1 = r1.get("metrics", {})
    msg1 = r1.get("decision", {}).get("assistantMessage", "")
    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 SQL ≤ 2 (no sector lookup)", metrics1.get("sqlCallCount", 0) <= 2, f"sql={metrics1.get('sqlCallCount')}")
    ok &= check(S, "T1 mentions 방산", any(kw in msg1 for kw in ["방산", "우주항공", "국방"]))
    ok &= check(S, "T1 has dataset", len(datasets1) > 0, f"{len(datasets1)} ds")
    ok &= check(S, "T1 dataset rows > 0", len(datasets1[0].get("rows", [])) > 0 if datasets1 else False)
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    q2 = "오늘까지의 시장상황과 세계 시장을 기반으로, 내일 급등할 가능성이 있는 종목은?"
    history = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history, sid, state1)
    print_turn(S, 2, q2, r2)

    msg2 = r2.get("decision", {}).get("assistantMessage", "")
    defense_kw = ["방산", "우주항공", "국방", "한화에어로", "현대로템", "LIG", "한화시스템", "풍산", "한화"]
    found = [kw for kw in defense_kw if kw in msg2]
    ok &= check(S, "T2 non-empty", bool(msg2.strip()) and "답변을 완성하지 못했습니다" not in msg2)
    ok &= check(S, "T2 maintains 방산 context", len(found) > 0, f"found: {found}")
    return ok


# ============================================================
def scenario_3():
    """Query reuse — 반도체 시총 → PER 추가"""
    print(f"\n{'='*70}\n  S3: Query reuse (반도체 시총 → PER)\n{'='*70}")
    S, sid = "3", "test-s3"

    q1 = "반도체 섹터 시가총액 상위 10개 종목 보여줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 has dataset", len(datasets1) > 0)
    ok &= check(S, "T1 rows ≥ 5", len(datasets1[0].get("rows", [])) >= 5 if datasets1 else False,
                f"{len(datasets1[0].get('rows', [])) if datasets1 else 0} rows")
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    q2 = "거기에 PER도 추가해서 보여줘"
    history = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history, sid, state1)
    print_turn(S, 2, q2, r2)

    metrics2 = r2.get("metrics", {})
    msg2 = r2.get("decision", {}).get("assistantMessage", "")
    datasets2 = r2.get("datasets", [])

    ok &= check(S, "T2 SQL ≤ 2 (reuse)", metrics2.get("sqlCallCount", 0) <= 2, f"sql={metrics2.get('sqlCallCount')}")
    ok &= check(S, "T2 non-empty", bool(msg2.strip()))
    ok &= check(S, "T2 has dataset", len(datasets2) > 0)
    if datasets2:
        cols_str = str(datasets2[0].get("columns", [])).lower()
        ok &= check(S, "T2 dataset has PER col", "per" in cols_str,
                    f"cols={[c.get('key','') for c in datasets2[0].get('columns',[])]}")
        ok &= check_dataset_values(S, datasets2[0])
    return ok


# ============================================================
def scenario_4():
    """Screening → Backtest → Condition check"""
    print(f"\n{'='*70}\n  S4: Screening → Backtest → Condition\n{'='*70}")
    S, sid = "4", "test-s4"

    # T1: screening
    q1 = "최근 20일 외국인 순매수 상위이면서 PBR 1 이하인 종목 스크리닝해줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 non-empty", bool(r1.get("decision", {}).get("assistantMessage", "").strip()))
    ok &= check(S, "T1 has dataset", len(datasets1) > 0)
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    # T2: backtest
    q2 = "이 조건으로 3개월 백테스트 해줘"
    history1 = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history1, sid, state1)
    print_turn(S, 2, q2, r2)

    metrics2 = r2.get("metrics", {})
    exec_log2 = r2.get("executionLog", [])
    datasets2 = r2.get("datasets", [])
    state2 = r2.get("stateSnapshot", {})

    ok &= check(S, "T2 ran backtest", "run_backtest" in str(exec_log2))
    ok &= check(S, "T2 has dataset", len(datasets2) > 0, f"{len(datasets2)} ds")
    if datasets2:
        ok &= check_dataset_values(S, datasets2[0])

    # Check backtest SQL conditions
    bt_sql = ""
    for item in state2.get("recent_tool_history", []):
        if item.get("tool") == "run_backtest":
            bt_sql = str(item.get("screening_sql", "")).lower()
    ok &= check(S, "T2 BT has foreign cond", "foreign" in bt_sql or "외국인" in bt_sql)
    ok &= check(S, "T2 BT has PBR cond", "pbr" in bt_sql or "6582" in bt_sql or "bps" in bt_sql)
    ok &= check(S, "T2 SQL ≤ 1 (reuse)", metrics2.get("sqlCallCount", 0) <= 1, f"sql={metrics2.get('sqlCallCount')}")

    # T3: condition check — no tool calls
    q3 = "이 백테스트에 포함된 조건이 정확히 뭐야?"
    history2 = build_history([
        {"question": q1, "result": r1},
        {"question": q2, "result": r2},
    ])
    r3 = run_turn(q3, history2, sid, state2)
    print_turn(S, 3, q3, r3)

    metrics3 = r3.get("metrics", {})
    msg3 = r3.get("decision", {}).get("assistantMessage", "")

    ok &= check(S, "T3 no tool calls", metrics3.get("toolCallCount", 0) == 0, f"tools={metrics3.get('toolCallCount')}")
    ok &= check(S, "T3 non-empty", bool(msg3.strip()) and "답변을 완성하지 못했습니다" not in msg3)
    return ok


# ============================================================
def scenario_5():
    """Backtest — 재무 조건 장기 (ROE+PER 3년)"""
    print(f"\n{'='*70}\n  S5: Backtest — 재무 조건 장기 (ROE+PER 3년)\n{'='*70}")
    S, sid = "5", "test-s5"

    q1 = "ROE 15% 이상이면서 PER 10 이하인 종목 스크리닝해줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 non-empty", bool(r1.get("decision", {}).get("assistantMessage", "").strip()))
    ok &= check(S, "T1 has dataset", len(datasets1) > 0)
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    q2 = "이 조건으로 3년 백테스트 해줘"
    history1 = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history1, sid, state1)
    print_turn(S, 2, q2, r2)

    metrics2 = r2.get("metrics", {})
    exec_log2 = r2.get("executionLog", [])
    datasets2 = r2.get("datasets", [])
    state2 = r2.get("stateSnapshot", {})

    ok &= check(S, "T2 ran backtest", "run_backtest" in str(exec_log2))
    ok &= check(S, "T2 has dataset", len(datasets2) > 0, f"{len(datasets2)} ds")
    if datasets2:
        ok &= check_dataset_values(S, datasets2[0])
        # 3년 quarterly = ~12 periods
        rows2 = datasets2[0].get("rows", [])
        ok &= check(S, "T2 rows ≥ 4 (multi-period)", len(rows2) >= 4, f"{len(rows2)} rows")
        has_nonzero = any(r.get("holdings", 0) > 0 for r in rows2)
        ok &= check(S, "T2 has holdings > 0", has_nonzero)

    # Check rebalance is quarterly (financial conditions)
    for item in state2.get("recent_tool_history", []):
        if item.get("tool") == "run_backtest":
            bt_months = item.get("months", 0)
            ok &= check(S, "T2 uses years (not months)", bt_months == 0, f"months={bt_months}")
    return ok


# ============================================================
def scenario_6():
    """Backtest — 수급 혼합 조건 데이터 제약 인지"""
    print(f"\n{'='*70}\n  S6: Backtest — 수급 혼합 조건 데이터 제약\n{'='*70}")
    S, sid = "6", "test-s6"

    q1 = "공매도 비중 높으면서 최근 20일 기관 순매수 전환된 종목 스크리닝해줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 non-empty", bool(r1.get("decision", {}).get("assistantMessage", "").strip()))
    ok &= check(S, "T1 has dataset", len(datasets1) > 0)
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    q2 = "1년 백테스트 해줘"
    history1 = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history1, sid, state1)
    print_turn(S, 2, q2, r2)

    exec_log2 = r2.get("executionLog", [])
    msg2 = r2.get("decision", {}).get("assistantMessage", "")
    datasets2 = r2.get("datasets", [])

    ok &= check(S, "T2 non-empty", bool(msg2.strip()))
    # Either ran backtest with adjusted period, or explained data limitation
    ran_bt = "run_backtest" in str(exec_log2)
    mentioned_limit = any(kw in msg2 for kw in ["데이터", "기간", "제한", "부족", "커버리지", "2024", "2026"])
    ok &= check(S, "T2 ran BT or explained limit", ran_bt or mentioned_limit,
                f"ran_bt={ran_bt}, mentioned_limit={mentioned_limit}")
    if datasets2:
        ok &= check_dataset_values(S, datasets2[0])
    return ok


# ============================================================
def scenario_7():
    """Backtest — 조건 변경 후 재백테스트"""
    print(f"\n{'='*70}\n  S7: Backtest — 조건 변경 재백테스트\n{'='*70}")
    S, sid = "7", "test-s7"

    q1 = "PBR 0.5 이하 종목 5년 백테스트 해줘"
    r1 = run_turn(q1, [], sid)
    print_turn(S, 1, q1, r1)

    exec_log1 = r1.get("executionLog", [])
    datasets1 = r1.get("datasets", [])
    state1 = r1.get("stateSnapshot", {})

    ok = True
    ok &= check(S, "T1 ran backtest", "run_backtest" in str(exec_log1))
    ok &= check(S, "T1 non-empty", bool(r1.get("decision", {}).get("assistantMessage", "").strip()))
    if datasets1:
        ok &= check_dataset_values(S, datasets1[0])

    q2 = "PBR 기준을 1 이하로 완화해서 다시 해줘"
    history1 = build_history([{"question": q1, "result": r1}])
    r2 = run_turn(q2, history1, sid, state1)
    print_turn(S, 2, q2, r2)

    exec_log2 = r2.get("executionLog", [])
    msg2 = r2.get("decision", {}).get("assistantMessage", "")
    datasets2 = r2.get("datasets", [])
    state2 = r2.get("stateSnapshot", {})

    ok &= check(S, "T2 ran backtest", "run_backtest" in str(exec_log2))
    ok &= check(S, "T2 non-empty", bool(msg2.strip()))
    if datasets2:
        ok &= check_dataset_values(S, datasets2[0])

    # Check that T2 backtest SQL has PBR 1 (not 0.5)
    for item in state2.get("recent_tool_history", []):
        if item.get("tool") == "run_backtest":
            bt_sql = str(item.get("screening_sql", "")).lower()
            # Should have 1.0 or 1, not 0.5
            has_pbr = "bps" in bt_sql or "6582" in bt_sql or "pbr" in bt_sql
            ok &= check(S, "T2 BT has PBR condition", has_pbr)
    return ok


# ============================================================
def scenario_8():
    """Heavy analysis — 삼성전자 DCF 자율 실행 (long-loop stress test)"""
    print(f"\n{'='*70}\n  S8: Heavy DCF (삼성전자)\n{'='*70}")
    S, sid = "8", "test-s8"

    q = (
        "너에게 주어진 모든 스킬과 툴, 데이터베이스, 뉴스데이터 등을 이용해서 "
        "세계 최고의 헤지펀드급 삼성전자 DCF를 진행해줘.\n"
        "나에게 출력할 것은\n"
        "1. 매출 추정 방법과 논리(근거)\n"
        "2. 세가지 시나리오의 핵심 트리거\n"
        "3. 현재 데이터 기반 각 시나리오의 확률을 제공해주고 가중평균한 DCF 결과 값\n"
        "모든 판단은 자율적으로 해서 최상의 결과를 나에게 줘."
    )
    r = run_turn(q, [], sid)
    print_turn(S, 1, q, r)

    msg = r.get("decision", {}).get("assistantMessage", "")
    exec_log = r.get("executionLog", [])
    metrics = r.get("metrics", {})

    ok = True
    ok &= check(S, "non-empty answer", bool(msg.strip()) and "답변을 완성하지 못했습니다" not in msg and len(msg.strip()) > 200)
    ok &= check(S, "no rate limit failure", "rate_limit" not in str(exec_log).lower() and "429" not in str(exec_log))
    ok &= check(S, "mentions 삼성전자", "삼성전자" in msg or "005930" in msg)
    ok &= check(S, "mentions DCF or 시나리오", any(kw in msg for kw in ["DCF", "시나리오", "현금흐름", "할인"]))
    ok &= check(S, "tool calls made", metrics.get("toolCallCount", 0) > 0, f"tools={metrics.get('toolCallCount')}")
    return ok


# ============================================================
def scenario_9():
    """Benchmark excess return — 코스피 대비 초과수익 (벤치마크 날짜 NULL 방지)"""
    print(f"\n{'='*70}\n  S9: Benchmark excess return\n{'='*70}")
    S, sid = "9", "test-s9"

    q = "코스피 대비 최근 6개월 초과수익이 높은 종목 상위 10개 알려줘"
    r = run_turn(q, [], sid)
    print_turn(S, 1, q, r)

    datasets = r.get("datasets", [])
    msg = r.get("decision", {}).get("assistantMessage", "")

    ok = True
    ok &= check(S, "has dataset", len(datasets) > 0)
    ok &= check(S, "non-empty answer", bool(msg.strip()))

    if datasets:
        ds = datasets[0]
        rows = ds.get("rows", [])
        cols = [c.get("key", "") for c in ds.get("columns", [])]

        ok &= check(S, "has rows", len(rows) >= 5, f"rows={len(rows)}")

        # Find benchmark/excess return columns (flexible naming)
        benchmark_cols = [c for c in cols if "kospi" in c.lower() or "benchmark" in c.lower() or "index_return" in c.lower()]
        excess_cols = [c for c in cols if "excess" in c.lower() or "초과" in c.lower()]

        ok &= check(S, "has benchmark column", len(benchmark_cols) > 0, f"found={benchmark_cols}")
        ok &= check(S, "has excess return column", len(excess_cols) > 0, f"found={excess_cols}")

        # Key check: benchmark and excess return values must NOT be NULL
        if benchmark_cols and rows:
            bcol = benchmark_cols[0]
            null_count = sum(1 for row in rows if row.get(bcol) is None or row.get(bcol) == "")
            ok &= check(S, "benchmark values not NULL", null_count == 0, f"null={null_count}/{len(rows)}")

        if excess_cols and rows:
            ecol = excess_cols[0]
            null_count = sum(1 for row in rows if row.get(ecol) is None or row.get(ecol) == "")
            ok &= check(S, "excess return values not NULL", null_count == 0, f"null={null_count}/{len(rows)}")

        check_dataset_values(S, ds)

    return ok


# ============================================================
if __name__ == "__main__":
    scenarios = {"1": scenario_1, "2": scenario_2, "3": scenario_3, "4": scenario_4,
                 "5": scenario_5, "6": scenario_6, "7": scenario_7, "8": scenario_8,
                 "9": scenario_9}
    targets = sys.argv[1:] if len(sys.argv) > 1 else sorted(scenarios.keys())
    results = {}

    wall_start = time.time()
    for key in targets:
        if key not in scenarios:
            print(f"Unknown scenario: {key}")
            continue
        try:
            results[key] = scenarios[key]()
        except Exception as e:
            print(f"  [ERROR] Scenario {key}: {e}")
            import traceback; traceback.print_exc()
            results[key] = False
    wall_elapsed = time.time() - wall_start

    # Summary
    total = len(_checks)
    passed = sum(1 for _, p, _ in _checks if p)
    failed = [(n, d) for n, p, d in _checks if not p]

    print(f"\n{'='*70}")
    print(f"  RESULTS ({passed}/{total} checks passed, {wall_elapsed:.0f}s total)")
    print(f"{'='*70}")

    for k, v in results.items():
        print(f"  Scenario {k}: [{'PASS' if v else 'FAIL'}]  {scenarios[k].__doc__}")

    if failed:
        print(f"\n  FAILED:")
        for n, d in failed:
            print(f"    ✗ {n}{' — '+d if d else ''}")

    print(f"\n  TIMING:")
    for label, ms in _timings:
        bar = "█" * (ms // 1000) + "▒" * ((ms % 1000) // 500)
        print(f"    {label:8s} {ms/1000:5.1f}s  {bar}")
    avg = sum(ms for _, ms in _timings) / len(_timings) if _timings else 0
    print(f"    {'avg':8s} {avg/1000:5.1f}s")

    # Save log file
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "test_scenarios")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"{ts}_{passed}of{total}.log")
    with open(log_path, "w") as f:
        f.write(f"date: {datetime.now().isoformat()}\n")
        f.write(f"result: {passed}/{total}\n")
        f.write(f"wall_time: {wall_elapsed:.0f}s\n\n")
        for name, p, detail in _checks:
            status = "PASS" if p else "FAIL"
            f.write(f"[{status}] {name}{' — '+detail if detail else ''}\n")
        f.write(f"\ntiming:\n")
        for label, ms in _timings:
            f.write(f"  {label:8s} {ms/1000:5.1f}s\n")
        f.write(f"  {'avg':8s} {avg/1000:5.1f}s\n")
    print(f"\n  Log saved: {log_path}")
