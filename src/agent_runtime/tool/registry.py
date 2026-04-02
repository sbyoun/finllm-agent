from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_runtime.conversation.state import ConversationState
from agent_runtime.tool.spec import ToolSpec
from agent_runtime.tool.tool import ToolDefinition

Resolver = Callable[[dict, ConversationState], Sequence[ToolDefinition]]


class ToolRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, Resolver] = {}

    def register(self, name: str, resolver: Resolver) -> None:
        self._registry[name] = resolver

    def resolve(self, tool_spec: ToolSpec, state: ConversationState) -> Sequence[ToolDefinition]:
        resolver = self._registry.get(tool_spec.name)
        if resolver is None:
            raise KeyError(f"Tool '{tool_spec.name}' is not registered")
        return resolver(tool_spec.params, state)

    def list_registered(self) -> list[str]:
        return sorted(self._registry.keys())
