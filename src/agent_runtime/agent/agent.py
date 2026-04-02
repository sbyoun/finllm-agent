from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.context.view import View
from agent_runtime.agent.base import AgentBase
from agent_runtime.conversation.local_conversation import LocalConversation
from agent_runtime.conversation.state import ConversationExecutionStatus
from agent_runtime.event.action import ActionEvent
from agent_runtime.event.message import MessageEvent, SystemPromptEvent
from agent_runtime.event.observation import AgentErrorEvent, ObservationEvent
from agent_runtime.llm.client import LLMToolCall, Message
from agent_runtime.tool.news.search_news import SearchNewsAction, SearchNewsObservation
from agent_runtime.tool.portfolio.get_portfolio import GetPortfolioObservation
from agent_runtime.tool.skills.load_skill import LoadSkillAction, LoadSkillObservation
from agent_runtime.tool.sql.run_sql import RunSQLAction, RunSQLObservation


def _tool_call_key(tool_call: LLMToolCall) -> str:
    return f"{tool_call.name}:{tool_call.arguments or '{}'}"


@dataclass(slots=True)
class Agent(AgentBase):
    _initialized: bool = False
    _tool_call_counts: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._tool_call_counts = {}

    def _extract_text_tool_calls(self, message: Message) -> Message:
        content = message.content or ""
        if "TOOL_CALL[" not in content:
            return message

        tool_calls = list(message.tool_calls)
        decoder = json.JSONDecoder()
        remaining_parts: list[str] = []
        cursor = 0

        while cursor < len(content):
            marker_index = content.find("TOOL_CALL[", cursor)
            if marker_index < 0:
                remaining_parts.append(content[cursor:])
                break

            remaining_parts.append(content[cursor:marker_index])

            name_end = content.find("]", marker_index + len("TOOL_CALL["))
            if name_end < 0:
                remaining_parts.append(content[marker_index:])
                break

            tool_name = content[marker_index + len("TOOL_CALL[") : name_end].strip()
            args_start = name_end + 1
            while args_start < len(content) and content[args_start].isspace():
                args_start += 1

            if args_start >= len(content) or content[args_start] != "{":
                remaining_parts.append(content[marker_index : name_end + 1])
                cursor = name_end + 1
                continue

            try:
                parsed_args, json_end = decoder.raw_decode(content[args_start:])
            except json.JSONDecodeError:
                remaining_parts.append(content[marker_index:])
                break

            tool_calls.append(
                LLMToolCall(
                    name=tool_name,
                    arguments=json.dumps(parsed_args, ensure_ascii=False),
                )
            )
            cursor = args_start + json_end

        cleaned_content = "".join(remaining_parts).strip()
        return Message(
            role=message.role,
            content=cleaned_content,
            tool_calls=tool_calls,
        )

    def _truncate_text(self, value: str, limit: int = 240) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _safe_json(self, value: Any, *, limit: int = 320) -> str:
        try:
            rendered = json.dumps(value, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            rendered = str(value)
        return self._truncate_text(rendered, limit=limit)

    def _append_recent_tool_history(
        self,
        conversation: LocalConversation,
        *,
        tool_name: str,
        summary: dict[str, Any],
    ) -> None:
        state = conversation.state
        history = list(state.get_agent_state("recent_tool_history", []))
        history.append(summary)
        state.set_agent_state("recent_tool_history", history[-12:])

    def _observation_summary(
        self,
        *,
        tool_name: str,
        action: object,
        observation: object,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"tool": tool_name}

        if isinstance(action, LoadSkillAction):
            summary["skill_name"] = action.skill_name
        elif isinstance(action, RunSQLAction):
            summary["sql"] = action.sql
            if action.title:
                summary["title"] = action.title
        elif isinstance(action, SearchNewsAction):
            summary["query"] = action.query

        if isinstance(observation, RunSQLObservation):
            summary["row_count"] = observation.row_count
            summary["columns"] = observation.columns
        elif isinstance(observation, SearchNewsObservation):
            summary["row_count"] = len(observation.rows)
            summary["preview_rows"] = observation.rows[:3]
        elif isinstance(observation, LoadSkillObservation):
            summary["skill_name"] = observation.skill_name
        elif isinstance(observation, GetPortfolioObservation):
            summary["row_count"] = len(observation.rows)
            summary["rows"] = observation.rows[:10]
        else:
            rows = getattr(observation, "rows", None)
            if isinstance(rows, list):
                summary["row_count"] = len(rows)
                summary["rows"] = rows[:10]

        return summary

    def _state_context(self, conversation: LocalConversation) -> str:
        agent_state = conversation.state.agent_state
        if not agent_state:
            return ""
        lines = [
            "",
            "",
            "## This Session's Accumulated Context",
            "",
            "Use the accumulated context from this session before deciding to call a tool again.",
        ]

        portfolio_state = agent_state.get("portfolio_state")
        if isinstance(portfolio_state, dict) and portfolio_state.get("loaded"):
            tickers = portfolio_state.get("tickers") or []
            row_count = portfolio_state.get("row_count")
            ticker_text = ", ".join(str(ticker) for ticker in tickers[:10]) if isinstance(tickers, list) else ""
            lines.append(f"- Portfolio already loaded: {row_count} holdings" + (f" [{ticker_text}]" if ticker_text else ""))

        loaded_skills = agent_state.get("loaded_skills")
        if isinstance(loaded_skills, list) and loaded_skills:
            lines.append(f"- Loaded skills: {', '.join(str(skill) for skill in loaded_skills[:10])}")

        last_sql = agent_state.get("last_successful_sql")
        if isinstance(last_sql, dict) and last_sql:
            columns = last_sql.get("columns") or []
            rows_preview = last_sql.get("rows_preview") or []
            title = str(last_sql.get("title") or "SQL Result")
            lines.append(f"- Last SQL result: {title}")
            if isinstance(columns, list) and columns:
                lines.append(f"  columns: {', '.join(str(column) for column in columns[:12])}")
            if isinstance(rows_preview, list) and rows_preview:
                lines.append(f"  preview: {self._safe_json(rows_preview[:3])}")

        previous_result = agent_state.get("previousResult")
        if isinstance(previous_result, dict):
            summary = str(previous_result.get("summary") or "").strip()
            if summary:
                lines.append(f"- Previous result summary: {self._truncate_text(summary, limit=300)}")

        recent_tool_history = agent_state.get("recent_tool_history")
        if isinstance(recent_tool_history, list) and recent_tool_history:
            rendered_tools: list[str] = []
            for item in recent_tool_history[-4:]:
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool") or "").strip()
                if tool:
                    rendered_tools.append(tool)
            if rendered_tools:
                lines.append(f"- Recent tool history: {' -> '.join(rendered_tools)}")

        lines.extend(
            [
                "",
                "If the needed holdings, schema context, or recent SQL result are already listed above, reuse them instead of reloading them.",
            ]
        )
        return "\n".join(lines)

    def _remember_observation(
        self,
        conversation: LocalConversation,
        *,
        tool_name: str,
        action: object,
        observation: object,
    ) -> None:
        state = conversation.state
        observation_summary = self._observation_summary(
            tool_name=tool_name,
            action=action,
            observation=observation,
        )
        state.set_agent_state("last_tool_result", observation_summary)
        self._append_recent_tool_history(
            conversation,
            tool_name=tool_name,
            summary=observation_summary,
        )

        if tool_name == "load_skill" and isinstance(action, LoadSkillAction) and isinstance(observation, LoadSkillObservation):
            loaded_skills = list(state.get_agent_state("loaded_skills", []))
            if action.skill_name not in loaded_skills:
                loaded_skills.append(action.skill_name)
            state.set_agent_state("loaded_skills", loaded_skills)
            return

        if tool_name == "run_sql" and isinstance(action, RunSQLAction) and isinstance(observation, RunSQLObservation):
            state.set_agent_state(
                "last_successful_sql",
                {
                    "title": action.title or "SQL Result",
                    "sql": action.sql,
                    "row_count": observation.row_count,
                    "columns": observation.columns,
                    "rows_preview": observation.rows[:10],
                },
            )
            return

        if tool_name == "search_news" and isinstance(action, SearchNewsAction) and isinstance(observation, SearchNewsObservation):
            state.set_agent_state(
                "last_news_result",
                {
                    "query": action.query,
                    "row_count": len(observation.rows),
                },
            )
            return

        if tool_name == "get_portfolio" and isinstance(observation, GetPortfolioObservation):
            rows = observation.rows[:20]
            state.set_agent_state(
                "portfolio_state",
                {
                    "loaded": True,
                    "row_count": len(observation.rows),
                    "rows": rows,
                    "tickers": [str(row.get("ticker", "")) for row in rows if row.get("ticker")],
                },
            )
            return

    def _remember_error(
        self,
        conversation: LocalConversation,
        *,
        tool_name: str,
        error: str,
    ) -> None:
        error_summary = {
            "tool": tool_name,
            "error": error,
        }
        conversation.state.set_agent_state(
            "last_error",
            error_summary,
        )
        self._append_recent_tool_history(
            conversation,
            tool_name=tool_name,
            summary=error_summary,
        )

    def _init_state(self, conversation: LocalConversation) -> None:
        if self._initialized:
            return
        conversation.state.event_log.append(
            SystemPromptEvent(
                system_prompt=self.system_prompt,
                dynamic_context=self.resolved_dynamic_context(),
                tools=[tool.as_llm_tool() for tool in self.tools],
            )
        )
        self._initialized = True

    def step(self, conversation: LocalConversation) -> None:
        self._init_state(conversation)
        view = View.from_events(list(conversation.state.event_log))
        if self.condenser is not None:
            condensation = self.condenser.condense(view)
            if condensation is not None:
                conversation.state.event_log.append(condensation)
                return
            view = View.from_events(list(conversation.state.event_log))
        messages = view.to_messages()
        if messages and messages[0].get("role") == "system":
            state_context = self._state_context(conversation)
            if state_context:
                messages[0]["content"] = f"{messages[0]['content']}{state_context}"
        llm_tools = [tool.as_llm_tool() for tool in self.tools]
        llm_response = self.llm.completion(messages=messages, tools=llm_tools)
        message = self._extract_text_tool_calls(llm_response.message)

        # Retry once on empty or incomplete response
        content_text = (message.content or "").strip()
        should_retry = (
            not message.tool_calls
            and (
                not content_text  # completely empty
                or (len(content_text) < 80 and "TOOL_CALL[" in content_text)  # failed text tool call
            )
        )
        if should_retry:
            llm_response = self.llm.completion(messages=messages, tools=llm_tools)
            message = self._extract_text_tool_calls(llm_response.message)

        if message.tool_calls:
            thought = message.content
            for tool_call in message.tool_calls:
                # Loop detection: block same tool+args after 3 repeats
                key = _tool_call_key(tool_call)
                self._tool_call_counts[key] = self._tool_call_counts.get(key, 0) + 1
                if self._tool_call_counts[key] > 3:
                    conversation.state.event_log.append(
                        AgentErrorEvent(
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            error=f"Blocked: repeated tool call ({self._tool_call_counts[key]}x). Move to a different action or provide a final answer.",
                        )
                    )
                    return

                tool = next((t for t in self.tools if t.name == tool_call.name), None)
                if tool is None:
                    conversation.state.event_log.append(
                        AgentErrorEvent(
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            error=f"Tool '{tool_call.name}' not found",
                        )
                    )
                    return

                try:
                    arguments = json.loads(tool_call.arguments) if tool_call.arguments else {}
                    action = tool.action_from_arguments(arguments)
                    action_event = ActionEvent(
                        tool_name=tool.name,
                        tool_call_id=tool_call.id,
                        thought=thought,
                        action=action,
                        llm_response_id=llm_response.id,
                    )
                    conversation.state.event_log.append(action_event)
                    observation = tool(action, conversation)
                    conversation.state.event_log.append(
                        ObservationEvent(
                            tool_name=tool.name,
                            tool_call_id=tool_call.id,
                            action_id=action_event.id,
                            observation=observation,
                        )
                    )
                    self._remember_observation(
                        conversation,
                        tool_name=tool.name,
                        action=action,
                        observation=observation,
                    )
                except Exception as exc:  # noqa: BLE001
                    conversation.state.event_log.append(
                        AgentErrorEvent(
                            tool_name=tool.name,
                            tool_call_id=tool_call.id,
                            error=str(exc),
                        )
                    )
                    self._remember_error(
                        conversation,
                        tool_name=tool.name,
                        error=str(exc),
                    )
                    return
            return

        conversation.state.event_log.append(
            MessageEvent(source="agent", role="assistant", content=message.content)
        )
        conversation.state.execution_status = ConversationExecutionStatus.FINISHED
