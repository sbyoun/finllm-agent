from __future__ import annotations

from dataclasses import asdict
import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

from agent_runtime.env import load_env
from agent_runtime.service import RuntimeAgentRequest, RuntimeMessageContext, run_agent_request


def resolve_repo_root() -> Path:
    configured = os.getenv("AGENT_REPO_ROOT") or os.getenv("APP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def append_turn_log(log_path: Path, question: str, new_events: list[dict], status: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now(UTC).isoformat(),
        "question": question,
        "status": status,
        "events": new_events,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def print_event(event: dict) -> None:
    event_type = event.get("event_type")
    if event_type == "message":
        print(f"[message:{event.get('source')}:{event.get('role')}] {event.get('content')}")
    elif event_type == "action":
        print(f"[action:{event.get('tool_name')}] thought={str(event.get('thought', ''))[:200]}")
        print(f"  action={event.get('action')}")
    elif event_type == "observation":
        print(f"[observation:{event.get('tool_name')}]")
        print(event.get("observation_text", ""))
    elif event_type == "agent_error":
        print(f"[error:{event.get('tool_name')}] {event.get('error')}")
    elif event_type == "conversation_state_update":
        print(
            f"[state:{event.get('operation')}] "
            f"{event.get('key')}={json.dumps(event.get('value'), ensure_ascii=False)}"
        )
    elif event_type == "condensation":
        print(
            f"[condensation] offset={event.get('summary_offset')} "
            f"forgotten={len(event.get('forgotten_event_ids') or [])}"
        )
        print(event.get("summary", ""))
    else:
        print(f"[event:{event_type}] {event}")


def main() -> None:
    load_env()
    repo_root = resolve_repo_root()
    history: list[RuntimeMessageContext] = []
    session_started_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = repo_root / "logs" / "chat_cli" / f"session-{session_started_at}.jsonl"

    print("financial-agent-runtime-py CLI")
    print("Type a question and press Enter.")
    print("Commands: /exit, /quit, /events")
    print(f"Logging to: {log_path}")

    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not question:
            continue
        if question in {"/exit", "/quit"}:
            print("bye")
            return
        if question == "/events":
            print(json.dumps([asdict(item) for item in history], ensure_ascii=False, indent=2))
            continue

        streamed_events: list[dict] = []
        # The CLI only manages interactive input/history. Execution always goes through the shared service entrypoint.
        try:
            result = run_agent_request(
                RuntimeAgentRequest(question=question, history=history, max_iterations=8),
                on_event=lambda event: None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime_error] {exc}")
            append_turn_log(
                log_path=log_path,
                question=question,
                new_events=[],
                status=f"error:{exc}",
            )
            continue
        for event in result.events:
            print_event(event)
            streamed_events.append(event)

        print(f"[status] {result.status}")
        print(f"[elapsed_ms] {result.elapsedMs}")
        history.append(RuntimeMessageContext(role="user", content=question))
        assistant_message = result.decision.assistantMessage.strip()
        if assistant_message:
            history.append(RuntimeMessageContext(role="assistant", content=assistant_message))
        append_turn_log(
            log_path=log_path,
            question=question,
            new_events=streamed_events,
            status=result.status,
        )


if __name__ == "__main__":
    sys.exit(main())
