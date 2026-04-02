# Result Schema

비교용 결과는 JSON 하나당 질문 1개 실행 결과를 저장한다.

필수 필드:

- `system`
  - 예: `python_mvp`, `ts_s0`, `ts_s2`
- `question_id`
- `question`
- `started_at`
- `elapsed_ms`
- `status`
- `event_count`
- `tool_calls`
- `final_answer`
- `artifacts`
  - tool observation 요약
- `notes`

예시:

```json
{
  "system": "python_mvp",
  "question_id": "news_nvda",
  "question": "엔비디아 최근 뉴스 정리해줘",
  "started_at": "2026-03-30T00:00:00Z",
  "elapsed_ms": 1840,
  "status": "finished",
  "event_count": 5,
  "tool_calls": ["search_news"],
  "final_answer": "최근 뉴스 3건을 찾았습니다 ...",
  "artifacts": {
    "observations": [
      {
        "tool_name": "search_news",
        "preview": "row_count=3 ..."
      }
    ]
  },
  "notes": ""
}
```
