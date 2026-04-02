from __future__ import annotations

from agent_runtime.agent.base import AgentBase
from agent_runtime.conversation.local_conversation import LocalConversation
from agent_runtime.conversation.state import ConversationState


def create_conversation(
    agent: AgentBase,
    *,
    max_iterations: int = 500,
    persistence_dir: str | None = None,
) -> LocalConversation:
    state = ConversationState(max_iterations=max_iterations)
    if persistence_dir is not None:
        state.event_log = state.event_log.__class__(persistence_dir=persistence_dir)
    return LocalConversation(agent=agent, state=state)
