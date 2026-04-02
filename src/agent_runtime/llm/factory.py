from __future__ import annotations

import os
from dataclasses import dataclass

from agent_runtime.env import require_env
from agent_runtime.llm.anthropic import AnthropicClient
from agent_runtime.llm.client import LLMClient
from agent_runtime.llm.gemini import GeminiClient
from agent_runtime.llm.openai_compatible import OpenAICompatibleClient


@dataclass(slots=True)
class RuntimeLlmConfig:
    model: str
    api_key: str
    base_url: str | None = None


def infer_provider(*, model: str, base_url: str | None = None) -> str:
    normalized_model = model.strip().lower()
    normalized_base_url = (base_url or "").strip().lower()

    if "gemini" in normalized_model or "generativelanguage.googleapis.com" in normalized_base_url:
        return "gemini"
    if "claude" in normalized_model or "anthropic" in normalized_base_url:
        return "anthropic"
    return "openai_compatible"


def create_llm_client(config: RuntimeLlmConfig) -> LLMClient:
    provider = infer_provider(model=config.model, base_url=config.base_url)
    if provider == "gemini":
        return GeminiClient(model=config.model, api_key=config.api_key)
    if provider == "anthropic":
        return AnthropicClient(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or "https://api.anthropic.com",
        )
    return OpenAICompatibleClient(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or "https://api.openai.com",
    )


def create_default_llm_client() -> LLMClient:
    model = os.getenv("MANAGED_GEMINI_MODEL", "").strip() or require_env("MANAGED_GEMINI_MODEL")
    api_key = os.getenv("MANAGED_GEMINI_API_KEY", "").strip() or require_env("MANAGED_GEMINI_API_KEY")
    base_url = os.getenv("MANAGED_GEMINI_BASE_URL", "").strip() or None
    return create_llm_client(
        RuntimeLlmConfig(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    )
