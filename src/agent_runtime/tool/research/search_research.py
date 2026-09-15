from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition

# FoldAlpha 자체 리서치(통념 검증 연재) 목록. 웹이 /research/index.json 으로 서빙하며 글을 고치면 재배포 없이 반영된다.
DEFAULT_INDEX_URL = "https://app.foldalpha.com/research/index.json"
_INDEX_TTL_SECONDS = 30 * 60
_BODY_MAX_CHARS = 6000
_MIN_SCORE = 0.08

_LOCK = threading.Lock()
_cache: dict[str, Any] = {"fetched_at": 0.0, "articles": []}


@dataclass(slots=True)
class SearchResearchAction(Action):
    query: str = ""
    limit: int = 3
    include_body: bool = False
    lang: str = "ko"


@dataclass(slots=True)
class SearchResearchObservation(Observation):
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_text(self) -> str:
        # 답을 쓰기 직전에 읽히는 자리라 인용 톤을 여기서 한 번 더 못 박는다.
        lines = [
            "note=FoldAlpha 자체 검증 글이다. 참고 사례로만 인용하고 확정 사실처럼 쓰지 않는다(검증이 틀릴 수 있다). "
            "인용할 때는 반드시 url 을 마크다운 링크로 붙이고, 글의 숫자를 내 분석 결과처럼 말하지 않는다."
        ]
        if self.error:
            lines.append(f"error={self.error}")
        lines.append(f"row_count={len(self.rows)}")
        for idx, row in enumerate(self.rows, start=1):
            lines.append(
                f"[{idx}] title={row.get('title', '')} | date={row.get('date', '')} | url={row.get('url', '')} | markdown_url={row.get('markdown_url', '')}"
            )
            summary = str(row.get("description") or "").replace("\n", " ").strip()
            if summary:
                lines.append(f"    finding={summary}")
            body = str(row.get("body") or "").strip()
            if body:
                lines.append("    body=<<<")
                lines.append(body)
                lines.append("    >>>")
        return "\n".join(lines)


class SearchResearchTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Korean or English keywords describing the trading belief or strategy (e.g. '외국인 순매수 따라사기', '급등주 추격', 'dip buying')."},
                "limit": {"type": "integer", "description": "Max articles to return (default 3)."},
                "include_body": {"type": "boolean", "description": "Also fetch the full markdown of the best match (for methodology/limitations follow-ups)."},
                "lang": {"type": "string", "description": "'ko' (default) or 'en'."},
            },
            "required": ["query"],
        }


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.lower())


def _bigrams(text: str) -> set[str]:
    normalized = _normalize(text)
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s\W_]+", text.lower()) if len(t) >= 2]


def score_article(query: str, article: dict[str, Any]) -> float:
    """질문과 글의 문자 2-gram 겹침(0~1) + 제목 토큰 일치 가산. 한국어 조사에 둔감하도록 문자 단위로 본다."""
    q_bigrams = _bigrams(query)
    if not q_bigrams:
        return 0.0
    haystack = " ".join(
        [str(article.get("title") or ""), str(article.get("description") or ""), str(article.get("slug") or "").replace("-", " ")]
    )
    overlap = len(q_bigrams & _bigrams(haystack)) / len(q_bigrams)
    title = str(article.get("title") or "").lower()
    token_hits = sum(1 for t in _tokens(query) if t in title)
    return overlap + 0.15 * token_hits


def rank_articles(query: str, articles: list[dict[str, Any]], *, limit: int, lang: str) -> list[dict[str, Any]]:
    candidates = [a for a in articles if str(a.get("lang") or "ko") == lang] or articles
    scored = [(score_article(query, a), a) for a in candidates]
    scored = [(s, a) for s, a in scored if s >= _MIN_SCORE]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{**a, "score": round(s, 3)} for s, a in scored[: max(1, min(limit, 10))]]


def _http_get(url: str, *, timeout: int = 15) -> bytes:
    request = Request(url, headers={"Accept": "application/json, text/markdown, */*", "User-Agent": "foldalpha-agent-runtime/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _load_index(*, force: bool = False) -> list[dict[str, Any]]:
    url = os.getenv("RESEARCH_INDEX_URL", "").strip() or DEFAULT_INDEX_URL
    with _LOCK:
        fresh = time.monotonic() - _cache["fetched_at"] < _INDEX_TTL_SECONDS
        if _cache["articles"] and fresh and not force:
            return list(_cache["articles"])
    payload = json.loads(_http_get(url).decode("utf-8"))
    articles = [
        {
            "slug": a.get("slug", ""),
            "lang": a.get("lang", "ko"),
            "title": a.get("title", ""),
            "description": a.get("description", ""),
            "date": a.get("date", ""),
            "url": a.get("url", ""),
            "markdown_url": a.get("markdownUrl") or a.get("markdown_url", ""),
        }
        for a in payload.get("articles", [])
    ]
    with _LOCK:
        _cache["fetched_at"] = time.monotonic()
        _cache["articles"] = articles
    return list(articles)


def _search(action: SearchResearchAction) -> SearchResearchObservation:
    query = action.query.strip()
    if not query:
        raise ValueError("search_research requires a non-empty query.")
    try:
        articles = _load_index()
    except (URLError, OSError, ValueError) as error:
        with _LOCK:
            stale = list(_cache["articles"])
        if not stale:
            return SearchResearchObservation(content=[], rows=[], error=f"research index unavailable: {error}")
        articles = stale
    rows = rank_articles(query, articles, limit=action.limit, lang=(action.lang or "ko").lower())
    if action.include_body and rows:
        top = rows[0]
        try:
            body = _http_get(top["markdown_url"]).decode("utf-8")
            top["body"] = body[:_BODY_MAX_CHARS] + ("\n…(truncated)" if len(body) > _BODY_MAX_CHARS else "")
        except (URLError, OSError) as error:
            top["body"] = f"(body fetch failed: {error})"
    return SearchResearchObservation(content=[], rows=rows)


def make_search_research_tool() -> SearchResearchTool:
    return SearchResearchTool(
        name="search_research",
        description=(
            "Search FoldAlpha's own research articles (통념 검증 series: following foreign/institutional flows, chasing spikes, "
            "buying dips, candle patterns, copy trading of insiders/Congress/13F, momentum & quality, backtest pitfalls, US→KR lead). "
            "Call it once when the user asks whether a trading belief, pattern, or strategy works, or asks about past verification results. "
            "Returns matching articles with title, one-line finding, and URL. Treat results as reference cases, not settled truth — "
            "the research can be wrong. Phrase as 'FoldAlpha 리서치에서는 …로 나왔습니다' and always include the article link."
        ),
        action_type=SearchResearchAction,
        observation_type=SearchResearchObservation,
        executor=lambda action, conversation=None: _search(action),  # noqa: ARG005
    )
