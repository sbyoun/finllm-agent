from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


@dataclass(slots=True)
class Message:
    role: str
    content: str
    tool_calls: list["LLMToolCall"] = field(default_factory=list)


@dataclass(slots=True)
class LLMToolCall:
    name: str
    arguments: str
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(slots=True)
class LLMUsage:
    """한 번 호출의 토큰. 공급자마다 다른 필드를 같은 뜻으로 맞춘다.

    input_tokens  : 입력 전체(캐시로 읽은 몫 포함)
    cached_tokens : 입력 중 캐시로 읽은 몫(할인 단가)
    cache_write_tokens : 캐시에 새로 쓴 몫(Anthropic 만, 할증 단가)
    output_tokens : 보이는 출력(thinking 제외)
    thinking_tokens : thinking/reasoning 출력(출력 단가로 과금)
    model_version : 응답이 알려 준 실제 모델(별칭 gemini-flash-latest 가 가리키는 것)
    """

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    model_version: str | None = None


@dataclass(slots=True)
class LLMResponse:
    message: Message
    id: str = field(default_factory=lambda: str(uuid4()))
    usage: LLMUsage | None = None


class LLMClient(Protocol):
    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        ...
