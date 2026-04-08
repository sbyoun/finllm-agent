# Financial Agent Runtime

A lightweight LLM agent runtime for financial data analysis, designed around the single-loop agent pattern observed in commercial coding agents (Claude Code, Codex CLI, Cursor, Windsurf).

This runtime powers [FoldAlpha](https://app.foldalpha.com), a financial analysis chat product that answers questions grounded in real market data.

## Architecture

```
User question
  -> Agent loop (LLM decides next action each step)
    -> Tool call (run_sql / search_news / get_portfolio)
    -> Observation (tool result)
    -> ... repeat until final answer
  -> Response: { assistantMessage, dataset, sql }
```

**Key design choices:**

- **Single-loop, no intent classification.** The LLM autonomously decides the next action at each step. No regex classifiers, completion checklists, or routing heuristics.
- **System-prompt-level context.** Domain schema (~3K tokens) is injected once into the system prompt, enabling API-level prefix caching across turns.
- **Minimal tool surface.** Five tools (`run_sql`, `search_news`, `get_portfolio`, `run_backtest`, `register_job`) instead of 9+ specialized resolvers.
- **Lightweight failure recovery.** Empty-response retry, loop detection (3x repeat block), and markdown table stripping.
- **Long-context compaction.** LLM-based conversation summarization kicks in automatically when history exceeds ~40K characters, retaining the last 6 turns verbatim.
- **Multi-dataset response.** A single agent run can return multiple datasets (e.g., backtest results + portfolio snapshot).

## Performance

Compared against a legacy domain-hardcoded planner with regex intent classification, per-turn state serialization, and 9 specialized tools:

| Metric | Legacy Planner | This Runtime |
|---|---|---|
| Avg latency (single-turn, 6 queries) | 30.0s | **12.7s** (2.4x) |
| Avg latency (multi-turn, 11 turns) | 29.5s | **14.4s** (2.1x) |
| Success rate | 91% | **100%** |
| Worst case | 96.9s (12-step loop) | 28.9s |
| Codebase size | ~700 lines | **~380 lines** |

See our [technical report](docs/paper2/) for detailed analysis and comparison with commercial agent architectures.

## Project Structure

```
src/agent_runtime/
  agent/          # Agent loop (step, tool dispatch, loop detection)
  api/            # FastAPI endpoints (/runs/sync, /runs/stream)
  context/        # View rendering, LLM-based condensation
  conversation/   # State management, event log
  event/          # Event types (message, action, observation, condensation)
  llm/            # Multi-provider clients (Gemini, Anthropic, OpenAI)
  prompt/         # System prompt, builder
  tool/           # Tool implementations
    sql/          #   Oracle DB query execution
    news/         #   Brave Search API
    portfolio/    #   Portfolio holdings (Supabase)
    backtest/     #   Factor strategy backtesting engine
    jobs/         #   Scheduled job registration (cron + Telegram alerts)
    skills/       #   Skill file loader
  service.py      # Main orchestration, result building
skills/           # Domain knowledge (schema guide, SQL patterns)
eval/             # Evaluation framework (single-turn, multi-turn)
scripts/          # Server control, CLI tools, smoke tests
```

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env
# Fill in API keys (Gemini/OpenAI/Anthropic, DB credentials, etc.)

# 2. Start the runtime
./scripts/serverctl.sh start
# or with explicit env:
./scripts/serverctl.sh --env production start

# 3. Health check
curl http://127.0.0.1:8010/health

# 4. Run a query
curl -X POST http://127.0.0.1:8010/runs/sync \
  -H "Content-Type: application/json" \
  -d '{"question": "Show me Samsung Electronics recent earnings"}'
```

## API

### `POST /runs/sync`

Synchronous execution. Returns full result when complete.

### `POST /runs/stream`

Server-Sent Events (SSE) streaming. Emits `status`, `tool`, `observation`, `final`, and `result` events.

### Request Schema

```json
{
  "question": "string (required)",
  "history": [{"role": "user|assistant", "content": "string"}],
  "stateSnapshot": {},
  "llmConfig": {"model": "string", "apiKey": "string", "baseUrl": "string"},
  "maxIterations": 25
}
```

### Response Schema

```json
{
  "decision": {
    "mode": "tool-result | answer-only | clarification",
    "assistantMessage": "string"
  },
  "dataset": {
    "title": "string",
    "columns": [{"key": "string", "label": "string"}],
    "rows": [{}]
  },
  "sql": "string | null",
  "metrics": {
    "loopCount": 0,
    "toolCallCount": 0,
    "sqlCallCount": 0,
    "newsCallCount": 0
  },
  "stateSnapshot": {},
  "elapsedMs": 0
}
```

## Tools

| Tool | Description |
|---|---|
| `run_sql` | Executes validated SQL against Oracle ADB. Returns structured dataset. |
| `search_news` | Brave Search API with per-call rate limiting (429 prevention). |
| `get_portfolio` | Fetches user portfolio holdings from Supabase. |
| `run_backtest` | Factor strategy backtesting engine. Quarterly rebalancing, 0.3% fee model, CAGR/MDD/Sharpe output. Results archived to DB. |
| `register_job` | Registers a scheduled job (natural language → cron). Sends results via Telegram. |

## Evaluation

```bash
# Single-turn eval (requires running runtime)
python eval/run_multiturn.py

# Provider matrix eval
python scripts/eval_provider_matrix.py --provider gemini
```

Evaluation queries cover: earnings analysis, news retrieval, company snapshots, cross-company comparison, factor screening, and multi-turn follow-ups.

## Design Principles

Informed by architectural patterns in commercial coding agents:

1. **Context density over orchestration complexity.** Rich context in the system prompt beats elaborate planning pipelines.
2. **Let the LLM route.** The model decides what tool to call and when to stop. No external classifiers needed.
3. **Cache-friendly context placement.** System prompt is stable across turns, enabling prefix caching.
4. **Minimal viable defenses.** Empty-response retry + loop detection + post-processing covers most failure modes.
5. **Conversation replay with condensation.** Full event history for context, with LLM-based summarization when it grows too large.

## References

- [Technical Report: From Domain-Hardcoded Planner to Lightweight Agent Loop](docs/paper2/)
- [Claude Code Architecture Analysis (leaked, 2026)](https://venturebeat.com/technology/claude-codes-source-code-appears-to-have-leaked-heres-what-we-know)
- [OpenAI Codex CLI (open-source)](https://github.com/openai/codex)
- [Anthropic: Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

## License

MIT
