from __future__ import annotations

import dataclasses
import json
import os
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_runtime.kis.quote import kis_quote
from agent_runtime.llm import RuntimeLlmConfig
from agent_runtime.service import RuntimeAgentRequest, RuntimeMessageContext, run_agent_request, run_agent_request_json


class MessageContextModel(BaseModel):
    role: str
    content: str


class LlmConfigModel(BaseModel):
    model: str
    apiKey: str
    baseUrl: str | None = None


class RunRequestModel(BaseModel):
    question: str
    userId: str | None = None
    sessionId: str | None = None
    history: list[MessageContextModel] = Field(default_factory=list)
    stateSnapshot: dict[str, Any] = Field(default_factory=dict)
    llmConfig: LlmConfigModel | None = None
    maxIterations: int = 25


def _to_runtime_request(body: RunRequestModel) -> RuntimeAgentRequest:
    return RuntimeAgentRequest(
        question=body.question,
        user_id=body.userId,
        session_id=body.sessionId,
        history=[RuntimeMessageContext(role=item.role, content=item.content) for item in body.history],
        state_snapshot=body.stateSnapshot,
        llm_config=RuntimeLlmConfig(
            model=body.llmConfig.model,
            api_key=body.llmConfig.apiKey,
            base_url=body.llmConfig.baseUrl,
        )
        if body.llmConfig
        else None,
        max_iterations=body.maxIterations,
    )


app = FastAPI(title="financial-agent-runtime-py")


def _resolve_repo_root() -> Path:
    configured = os.getenv("AGENT_REPO_ROOT") or os.getenv("APP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/kis/quote")
def kis_quote_endpoint(symbol: str) -> JSONResponse:
    try:
        quote = kis_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(quote)


@app.post("/runs/sync")
def run_sync(body: RunRequestModel) -> JSONResponse:
    result = run_agent_request_json(_to_runtime_request(body))
    return JSONResponse(result)


@app.post("/runs/stream")
def run_stream(body: RunRequestModel) -> StreamingResponse:
    request = _to_runtime_request(body)
    repo_root = _resolve_repo_root()

    def generate():
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        result_holder: dict[str, Any] = {}

        def on_event(event: dict[str, Any]) -> None:
            event_queue.put(event)

        def worker() -> None:
            try:
                result_holder["result"] = run_agent_request(
                    request,
                    on_event=on_event,
                    repo_root=repo_root,
                )
            except Exception as exc:  # noqa: BLE001
                result_holder["error"] = str(exc)
            finally:
                event_queue.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            item = event_queue.get()
            if item is None:
                break
            yield f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

        if "error" in result_holder:
            payload = {"type": "error", "message": result_holder["error"]}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        result = result_holder["result"]
        final_result = {
            "runId": result.runId,
            "decision": dataclasses.asdict(result.decision),
            "dataset": dataclasses.asdict(result.dataset) if result.dataset else None,
            "datasets": [dataclasses.asdict(item) for item in result.datasets],
            "sql": result.sql,
            "sqlScripts": result.sqlScripts,
            "metrics": dataclasses.asdict(result.metrics),
            "executionLog": result.executionLog,
            "events": result.events,
            "elapsedMs": result.elapsedMs,
            "status": result.status,
            "stateSnapshot": result.stateSnapshot,
            "tracePath": result.tracePath,
        }
        yield f"event: result\ndata: {json.dumps(final_result, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
