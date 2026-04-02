from __future__ import annotations

import json
from uuid import UUID
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.env import require_env
from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


@dataclass(slots=True)
class GetPortfolioAction(Action):
    user_id: str = ""
    session_id: str = ""


@dataclass(slots=True)
class GetPortfolioObservation(Observation):
    user_id: str = ""
    session_id: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_text(self) -> str:
        return "\n".join(
            [
                f"user_id={self.user_id}",
                f"session_id={self.session_id}",
                f"row_count={len(self.rows)}",
                f"rows={self.rows}",
            ]
        )


class GetPortfolioTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }


def _fetch_portfolio_rows(*, user_id: str, session_id: str) -> list[dict[str, Any]]:
    base_url = require_env("NEXT_PUBLIC_SUPABASE_URL").rstrip("/")
    service_role_key = require_env("SUPABASE_SERVICE_ROLE_KEY")

    params = urlencode(
        {
            "select": "ticker,name,weight,position_order",
            "user_id": f"eq.{user_id}",
            "session_id": f"eq.{session_id}",
            "order": "position_order.asc",
        }
    )
    request = Request(
        f"{base_url}/rest/v1/portfolio_holdings?{params}",
        headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"get_portfolio supabase query failed: HTTP {exc.code} {details}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected portfolio response payload.")
    return payload


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def make_get_portfolio_tool() -> GetPortfolioTool:
    def _execute(action: GetPortfolioAction, conversation: object | None = None) -> GetPortfolioObservation:
        conversation_state = getattr(conversation, "state", None)
        stored_user_id = ""
        stored_session_id = ""
        if conversation_state is not None:
            stored_user_id = str(conversation_state.get_agent_state("user_id", "") or "").strip()
            stored_session_id = str(conversation_state.get_agent_state("session_id", "") or "").strip()

        user_id = stored_user_id
        session_id = stored_session_id
        if not user_id:
            raise ValueError("get_portfolio requires a user_id in the request context.")
        if not session_id:
            raise ValueError("get_portfolio requires a session_id in the request context.")
        if not _is_uuid(user_id):
            return GetPortfolioObservation(
                content=[],
                user_id=user_id,
                session_id=session_id,
                rows=[],
            )

        rows = _fetch_portfolio_rows(user_id=user_id, session_id=session_id)
        return GetPortfolioObservation(
            content=[],
            user_id=user_id,
            session_id=session_id,
            rows=rows,
        )

    return GetPortfolioTool(
        name="get_portfolio",
        description="Load the saved portfolio holdings for the current authenticated user and chat session.",
        action_type=GetPortfolioAction,
        observation_type=GetPortfolioObservation,
        executor=_execute,
    )
