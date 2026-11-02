"""실행(run) 한 번 동안의 LLM 토큰을 모아 llm_usage_events 에 한 줄로 남긴다.

원가 측정용이다(질문 1건 평균·p95 원가 → 구독 상한·무료 한도 결정).
단가는 저장하지 않는다 — 토큰 원본만 남기고 원가는 집계할 때 단가표로 계산한다
(별칭 모델이 가리키는 버전·단가가 바뀌어도 다시 계산할 수 있게).

기록은 best-effort: 실패해도 사용자 응답을 막지 않는다. DB 에 못 쓰면 로컬 JSONL 에 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from agent_runtime.llm.client import LLMClient, LLMResponse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UsageTotals:
    calls: int = 0
    calls_without_usage: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    model_versions: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "callsWithoutUsage": self.calls_without_usage,
            "failedCalls": self.failed_calls,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "thinkingTokens": self.thinking_tokens,
            "cachedTokens": self.cached_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
            "modelVersions": dict(self.model_versions),
        }


class MeteredLLMClient:
    """LLMClient 를 감싸 호출마다 usage 를 더한다. 인터페이스는 그대로."""

    def __init__(self, inner: LLMClient, *, provider: str, model: str) -> None:
        self.inner = inner
        self.provider = provider
        self.model = model
        self.totals = UsageTotals()
        self._lock = threading.Lock()

    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        try:
            response = self.inner.completion(messages=messages, tools=tools)
        except Exception:
            # 재시도까지 다 실패한 호출. 공급자는 보통 과금하지 않지만 횟수는 남긴다.
            with self._lock:
                self.totals.failed_calls += 1
            raise
        with self._lock:
            t = self.totals
            t.calls += 1
            usage = response.usage
            if usage is None:
                t.calls_without_usage += 1
            else:
                t.input_tokens += usage.input_tokens
                t.output_tokens += usage.output_tokens
                t.thinking_tokens += usage.thinking_tokens
                t.cached_tokens += usage.cached_tokens
                t.cache_write_tokens += usage.cache_write_tokens
                version = usage.model_version or self.model
                t.model_versions[version] = t.model_versions.get(version, 0) + 1
        return response

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"provider": self.provider, "model": self.model, **self.totals.to_dict()}


def _primary_model_version(summary: dict[str, Any]) -> str | None:
    versions: dict[str, int] = summary.get("modelVersions") or {}
    if not versions:
        return None
    return max(versions.items(), key=lambda kv: kv[1])[0]


def build_usage_row(
    *,
    run_id: str,
    summary: dict[str, Any],
    user_id: str | None,
    session_id: str | None,
    source: str | None,
    billing: str | None,
    plan_code: str | None,
    status: str,
    elapsed_ms: int | None,
    loop_count: int | None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "user_id": user_id,
        "session_id": session_id,
        "source": source or "unknown",
        "billing": billing or "unknown",
        "plan_code": plan_code,
        "provider": summary.get("provider"),
        "model": summary.get("model"),
        "model_version": _primary_model_version(summary),
        "calls": summary.get("calls", 0),
        "failed_calls": summary.get("failedCalls", 0),
        "calls_without_usage": summary.get("callsWithoutUsage", 0),
        "input_tokens": summary.get("inputTokens", 0),
        "output_tokens": summary.get("outputTokens", 0),
        "thinking_tokens": summary.get("thinkingTokens", 0),
        "cached_tokens": summary.get("cachedTokens", 0),
        "cache_write_tokens": summary.get("cacheWriteTokens", 0),
        "loop_count": loop_count,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "detail": {"modelVersions": summary.get("modelVersions") or {}},
    }


def record_usage(row: dict[str, Any], *, repo_root: Path) -> None:
    """Supabase llm_usage_events 에 쓴다. 실패하면 logs/llm_usage.jsonl 에 남긴다."""
    if os.getenv("LLM_USAGE_RECORDING", "on").strip().lower() == "off":
        return
    base_url = (os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    error: str | None = None
    if base_url and key:
        request = Request(
            f"{base_url}/rest/v1/llm_usage_events",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            data=json.dumps(row, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        try:
            with urlopen(request, timeout=5):
                return
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:300]
    else:
        error = "supabase env missing"
    logger.warning("llm usage insert failed (%s); writing local ledger", error)
    try:
        ledger = repo_root / "logs" / "llm_usage.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "logged_at": datetime.now(UTC).isoformat(), "insert_error": error}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("llm usage local ledger write failed")
