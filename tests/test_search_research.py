from agent_runtime.tool.research.search_research import (
    SearchResearchAction,
    SearchResearchObservation,
    make_search_research_tool,
    rank_articles,
    score_article,
)

ARTICLES = [
    {"slug": "foreign-flow-following", "lang": "ko", "title": "외국인 순매수 따라사기는 수익이 나는가 — 한국장 4년 986거래일 검증",
     "description": "외국인 5일 순매수 상위 20종목을 따라 사면 20일 뒤 시장 대비 +1.81%p. 그런데 외국인이 집중 매도한 종목도 +1.74%p 올랐다. 롱숏 차이 +0.03%p — 매수 방향 자체에는 정보가 없었다.",
     "date": "2026-09-01", "url": "https://app.foldalpha.com/research/foreign-flow-following", "markdown_url": "https://app.foldalpha.com/research/foreign-flow-following/report.md"},
    {"slug": "spike-chasing", "lang": "ko", "title": "급등주를 따라 사면 어떻게 되나 — 22,474건 전수 검증",
     "description": "당일 +13% 이상 급등한 종목을 종가에 추격 매수하면 20일 뒤 시장 대비 -7.5%p(중앙값 -8.9%), 시장을 이긴 비율 34.8%.",
     "date": "2026-09-01", "url": "https://app.foldalpha.com/research/spike-chasing", "markdown_url": "https://app.foldalpha.com/research/spike-chasing/report.md"},
    {"slug": "dip-buying", "lang": "ko", "title": "급락한 주식을 주우면 — 낙폭 과대 매수 5만 건 검증",
     "description": "5일간 -15% 이상 빠진 종목을 사면 20일 뒤 시장 대비 -3.8%p, 시장을 이긴 비율 39.4%.",
     "date": "2026-09-01", "url": "https://app.foldalpha.com/research/dip-buying", "markdown_url": "https://app.foldalpha.com/research/dip-buying/report.md"},
    {"slug": "spike-chasing", "lang": "en", "title": "What happens if you chase surging stocks",
     "description": "Buying at the close after a +13% day underperforms the market by 7.5pp over 20 days.",
     "date": "2026-09-01", "url": "https://app.foldalpha.com/en/research/spike-chasing", "markdown_url": "https://app.foldalpha.com/en/research/spike-chasing/report.md"},
]


def test_korean_query_ranks_matching_article_first():
    rows = rank_articles("외국인이 많이 산 종목 따라 사면 오를 확률이 높아?", ARTICLES, limit=3, lang="ko")
    assert rows, "expected at least one match"
    assert rows[0]["slug"] == "foreign-flow-following"
    assert rows[0]["url"].endswith("/research/foreign-flow-following")


def test_spike_query_prefers_spike_article_and_respects_lang():
    rows = rank_articles("급등주 추격 매수", ARTICLES, limit=2, lang="ko")
    assert rows[0]["slug"] == "spike-chasing"
    assert all(r["lang"] == "ko" for r in rows)
    en = rank_articles("chase surging stocks", ARTICLES, limit=2, lang="en")
    assert en and en[0]["lang"] == "en"


def test_unrelated_query_returns_nothing():
    assert rank_articles("배당 성향과 부채비율 계산 방법", ARTICLES, limit=3, lang="ko") == [] or \
        rank_articles("배당 성향과 부채비율 계산 방법", ARTICLES, limit=3, lang="ko")[0]["score"] < 0.2
    assert score_article("x", ARTICLES[0]) == 0.0


def test_observation_text_carries_tone_note_and_links():
    obs = SearchResearchObservation(content=[], rows=rank_articles("급등주", ARTICLES, limit=1, lang="ko"))
    text = obs.to_text()
    assert "참고 사례" in text and "row_count=1" in text
    assert "url=https://app.foldalpha.com/research/spike-chasing" in text
    assert "finding=" in text


def test_tool_schema_and_llm_shape():
    tool = make_search_research_tool()
    llm_tool = tool.as_llm_tool()
    assert llm_tool["function"]["name"] == "search_research"
    assert "query" in llm_tool["function"]["parameters"]["required"]
    action = tool.action_from_arguments({"query": "급등주", "limit": 2})
    assert isinstance(action, SearchResearchAction) and action.limit == 2
