#!/usr/bin/env python3
"""Multi-turn conversation evaluation for Python runtime API."""

import json
import time
import urllib.request
from pathlib import Path

RUNTIME_URL = "http://127.0.0.1:8001/runs/sync"
SCENARIOS_PATH = Path(__file__).parent / "multiturn_scenarios.json"


def call_runtime(question: str, history: list, state_snapshot: dict) -> dict:
    payload = json.dumps({
        "question": question,
        "history": history,
        "stateSnapshot": state_snapshot,
        "maxIterations": 8,
    }).encode()

    req = urllib.request.Request(
        RUNTIME_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    elapsed_ms = round((time.time() - start) * 1000)
    return {**data, "_wall_ms": elapsed_ms}


def run_scenario(scenario: dict) -> list[dict]:
    sid = scenario["id"]
    name = scenario["name"]
    turns = scenario["turns"]

    print(f"\n{'='*70}")
    print(f"SCENARIO: {name} ({sid})")
    print(f"{'='*70}")

    history: list[dict] = []
    state_snapshot: dict = {}
    turn_results = []

    for i, turn in enumerate(turns):
        q = turn["question"]
        print(f"\n  --- Turn {i+1}: {q} ---")

        result = call_runtime(q, history, state_snapshot)

        wall_ms = result["_wall_ms"]
        internal_ms = result.get("elapsedMs", 0)
        mode = result.get("decision", {}).get("mode", "?")
        msg = result.get("decision", {}).get("assistantMessage", "")
        m = result.get("metrics", {})
        ds = result.get("dataset")
        sql = result.get("sql")
        new_state = result.get("stateSnapshot") or {}

        rows = len(ds.get("rows", [])) if ds else 0
        cols = [c["key"] for c in ds.get("columns", [])] if ds else []

        print(f"  Time: {wall_ms}ms | Mode: {mode} | Loops: {m.get('loopCount',0)} | Tools: {m.get('toolCallCount',0)} | SQL: {m.get('sqlCallCount',0)} | News: {m.get('newsCallCount',0)}")
        if ds:
            print(f"  Dataset: {rows} rows, cols: {cols[:6]}")
        print(f"  Answer ({len(msg)} chars): {msg[:200]}...")

        # Check follow-up quality signals
        reused_state = bool(state_snapshot) and bool(new_state)
        if i > 0:
            # Check if previous context was referenced
            prev_msg = turn_results[-1]["msg"]
            print(f"  [Follow-up] State carried: {bool(new_state)} | Previous state existed: {bool(state_snapshot)}")

        turn_results.append({
            "turn": i + 1,
            "question": q,
            "wall_ms": wall_ms,
            "internal_ms": internal_ms,
            "mode": mode,
            "loops": m.get("loopCount", 0),
            "tools": m.get("toolCallCount", 0),
            "sql_calls": m.get("sqlCallCount", 0),
            "news_calls": m.get("newsCallCount", 0),
            "rows": rows,
            "msg_len": len(msg),
            "msg": msg,
            "has_dataset": ds is not None,
            "state_keys": list(new_state.keys()) if new_state else [],
        })

        # Build history for next turn
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": msg})

        # Carry state forward
        state_snapshot = new_state

    return turn_results


def main():
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    all_results = {}

    for scenario in scenarios:
        turn_results = run_scenario(scenario)
        all_results[scenario["id"]] = {
            "name": scenario["name"],
            "turns": turn_results,
        }

    # Summary
    print(f"\n\n{'='*70}")
    print("MULTI-TURN SUMMARY")
    print(f"{'='*70}")
    fmt = "{:<30} {:>4} {:>8} {:>6} {:>5} {:>5} {:>5} {:>6}"
    print(fmt.format("Scenario / Turn", "Turn", "Time", "Mode", "Loops", "Tools", "Rows", "Chars"))
    print("-" * 80)

    for sid, sdata in all_results.items():
        print(f"\n  [{sid}] {sdata['name']}")
        for t in sdata["turns"]:
            q_short = t["question"][:28]
            print(fmt.format(
                f"  {q_short}",
                t["turn"],
                f"{t['wall_ms']}ms",
                t["mode"][:6],
                t["loops"],
                t["tools"],
                t["rows"],
                t["msg_len"],
            ))

    # Save results
    out_path = Path(__file__).parent.parent / "results" / f"multiturn_{int(time.time())}.json"
    out_path.parent.mkdir(exist_ok=True)
    # Remove msg from saved results (too long)
    save_data = {}
    for sid, sdata in all_results.items():
        save_data[sid] = {
            "name": sdata["name"],
            "turns": [{k: v for k, v in t.items() if k != "msg"} for t in sdata["turns"]],
        }
    out_path.write_text(json.dumps(save_data, ensure_ascii=False, indent=2))
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
