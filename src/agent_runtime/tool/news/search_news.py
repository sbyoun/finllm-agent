from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_runtime.env import require_env
from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition

# Brave Search API rate limit guard: enforce minimum interval between calls
_NEWS_LOCK = threading.Lock()
_NEWS_MIN_INTERVAL = 2.0  # seconds
_news_last_called: float = 0.0


@dataclass(slots=True)
class SearchNewsAction(Action):
    query: str = ""
    country: str = "KR"
    search_lang: str = "ko"
    freshness: str = "pw"
    count: int = 8


@dataclass(slots=True)
class SearchNewsObservation(Observation):
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_text(self) -> str:
        preview = self.rows[:5]
        return "\n".join(
            [
                f"row_count={len(self.rows)}",
                f"preview_rows={preview}",
            ]
        )


class SearchNewsTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "country": {"type": "string"},
                "search_lang": {"type": "string"},
                "freshness": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["query"],
        }


def _search_brave_news(action: SearchNewsAction) -> SearchNewsObservation:
    global _news_last_called
    with _NEWS_LOCK:
        elapsed = time.monotonic() - _news_last_called
        if elapsed < _NEWS_MIN_INTERVAL:
            time.sleep(_NEWS_MIN_INTERVAL - elapsed)
        _news_last_called = time.monotonic()

    api_key = require_env("BRAVE_SEARCH_API_KEY")
    query = action.query.strip()
    if not query:
        raise ValueError("search_news requires a non-empty query.")

    params = urlencode(
        {
            "q": query,
            "country": action.country or "KR",
            "search_lang": action.search_lang or "ko",
            "freshness": action.freshness or "pw",
            "count": str(max(1, min(action.count, 20))),
            "extra_snippets": "true",
        }
    )

    request = Request(
        f"https://api.search.brave.com/res/v1/news/search?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )

    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results = payload.get("results") or payload.get("news", {}).get("results") or []
    rows = [
        {
            "title": item.get("title", ""),
            "source": (item.get("meta_url") or {}).get("hostname", ""),
            "published_at": item.get("page_age") or item.get("age") or "",
            "description": item.get("description", ""),
            "url": item.get("url", ""),
        }
        for item in results
    ]
    return SearchNewsObservation(content=[], rows=rows)


def make_search_news_tool() -> SearchNewsTool:
    return SearchNewsTool(
        name="search_news",
        description="Search recent news and return structured news rows. IMPORTANT: call this tool one at a time — do NOT call multiple search_news in parallel. Sequential calls only.",
        action_type=SearchNewsAction,
        observation_type=SearchNewsObservation,
        executor=lambda action, conversation=None: _search_brave_news(action),  # noqa: ARG005
    )
