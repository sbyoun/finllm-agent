"""get_forward_test tool: retrieve forward test status and recent snapshots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def _supabase_request(path: str) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


@dataclass(slots=True)
class GetForwardTestAction(Action):
    forward_test_id: str | None = None  # specific test, or list all for user
    include_snapshots: bool = True

    def to_arguments_json(self) -> str:
        d: dict[str, Any] = {"include_snapshots": self.include_snapshots}
        if self.forward_test_id:
            d["forward_test_id"] = self.forward_test_id
        return json.dumps(d, ensure_ascii=False)


@dataclass(slots=True)
class GetForwardTestObservation(Observation):
    success: bool = False
    message: str = ""
    tests: list[dict] | None = None

    def to_text(self) -> str:
        if not self.success:
            return f"조회 실패: {self.message}"
        if not self.tests:
            return "활성 포워드 테스트가 없습니다."

        lines: list[str] = []
        for t in self.tests:
            status_emoji = {"active": "▶", "paused": "⏸", "stopped": "⏹"}.get(t.get("status", ""), "?")
            line = f"{status_emoji} {t['name']} ({t['strategy_type']}) — {t['universe']}"

            snapshots = t.get("snapshots", [])
            if snapshots:
                latest = snapshots[0]
                line += f" | 수익률: {latest.get('return_pct', 0):+.2f}% | 종목: {len(latest.get('holdings', []))}개"

            lines.append(line)

            if snapshots and len(snapshots) > 0:
                latest = snapshots[0]
                holdings = latest.get("holdings", [])
                if holdings:
                    top = holdings[:5]
                    names = ", ".join(h.get("name", h.get("symbol", "?")) for h in top)
                    suffix = f" 외 {len(holdings) - 5}개" if len(holdings) > 5 else ""
                    lines.append(f"  보유: {names}{suffix}")

        return "\n".join(lines)


def _execute(action: GetForwardTestAction, conversation: Any) -> GetForwardTestObservation:
    state = conversation.state
    user_id = state.get_agent_state("user_id")
    if not user_id:
        return GetForwardTestObservation(
            success=False, message="회원 전용 기능입니다."
        )

    try:
        if action.forward_test_id:
            path = f"forward_tests?id=eq.{action.forward_test_id}&user_id=eq.{user_id}&select=*"
        else:
            path = f"forward_tests?user_id=eq.{user_id}&select=*&order=created_at.desc&limit=20"

        tests = _supabase_request(path)
        if not tests:
            return GetForwardTestObservation(success=True, message="", tests=[])

        # Fetch recent snapshots for each test
        if action.include_snapshots:
            for t in tests:
                snap_path = (
                    f"forward_snapshots?forward_test_id=eq.{t['id']}"
                    f"&select=holdings,cash,total_value,return_pct,trades,reasoning,snapshot_at"
                    f"&order=snapshot_at.desc&limit=5"
                )
                t["snapshots"] = _supabase_request(snap_path)

        return GetForwardTestObservation(success=True, message="", tests=tests)

    except Exception as exc:
        return GetForwardTestObservation(
            success=False, message=f"조회 실패: {exc}"
        )


@dataclass(slots=True)
class GetForwardTestTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "forward_test_id": {
                    "type": "string",
                    "description": "특정 포워드 테스트 ID. 생략하면 전체 목록 조회.",
                },
                "include_snapshots": {
                    "type": "boolean",
                    "description": "최근 스냅샷 포함 여부 (기본: true)",
                },
            },
            "required": [],
        }


def make_get_forward_test_tool() -> GetForwardTestTool:
    return GetForwardTestTool(
        name="get_forward_test",
        description=(
            "포워드 테스트 현황을 조회합니다. "
            "특정 ID를 지정하면 상세 조회, 생략하면 전체 목록을 반환합니다. "
            "최근 스냅샷(보유 종목, 수익률, 매매 내역)을 포함합니다."
        ),
        action_type=GetForwardTestAction,
        observation_type=GetForwardTestObservation,
        executor=_execute,
    )
