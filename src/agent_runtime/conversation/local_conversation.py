from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.agent.base import AgentBase
from agent_runtime.conversation.base import BaseConversation
from agent_runtime.conversation.state import ConversationExecutionStatus, ConversationState
from agent_runtime.event.message import MessageEvent


@dataclass(slots=True)
class LocalConversation(BaseConversation):
    agent: AgentBase
    state: ConversationState

    def send_message(self, message: str) -> None:
        if self.state.execution_status in (
            ConversationExecutionStatus.FINISHED,
            ConversationExecutionStatus.ERROR,
        ):
            self.state.execution_status = ConversationExecutionStatus.IDLE
        self.state.event_log.append(
            MessageEvent(source="user", role="user", content=message)
        )

    def run(self) -> None:
        if self.state.execution_status in (
            ConversationExecutionStatus.IDLE,
            ConversationExecutionStatus.PAUSED,
            ConversationExecutionStatus.ERROR,
        ):
            self.state.execution_status = ConversationExecutionStatus.RUNNING

        iteration = 0
        while self.state.execution_status == ConversationExecutionStatus.RUNNING:
            self.agent.step(self)
            iteration += 1
            if iteration >= self.state.max_iterations:
                self.state.execution_status = ConversationExecutionStatus.ERROR
                break
