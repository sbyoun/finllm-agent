from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runtime.env import load_env
from agent_runtime.llm.factory import RuntimeLlmConfig
from agent_runtime.service import RuntimeAgentRequest, run_agent_request


def resolve_repo_root() -> Path:
    configured = os.getenv("AGENT_REPO_ROOT") or os.getenv("APP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


REPO_ROOT = resolve_repo_root()
DEFAULT_QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.json"
DEFAULT_RESULTS_DIR = REPO_ROOT / "results"


def load_questions(questions_path: Path) -> list[dict[str, Any]]:
    return json.loads(questions_path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run provider matrix evaluation against the Python runtime.")
    parser.add_argument(
        "--questions-file",
        type=Path,
        default=DEFAULT_QUESTIONS_PATH,
        help="Path to questions JSON file.",
    )
    parser.add_argument(
        "--question-id",
        action="append",
        dest="question_ids",
        default=[],
        help="Run only the matching question id. Repeatable.",
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        choices=["gemini", "openai", "anthropic"],
        default=[],
        help="Run only selected providers. Repeatable.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum tool/LLM loop iterations per question.",
    )
    parser.add_argument(
        "--output-prefix",
        default="provider_eval",
        help="Result filename prefix under results/.",
    )
    return parser.parse_args()


def build_provider_configs() -> dict[str, RuntimeLlmConfig]:
    configs: dict[str, RuntimeLlmConfig] = {}

    if key := _env("MANAGED_GEMINI_API_KEY"):
        configs["gemini"] = RuntimeLlmConfig(
            model=_env("MANAGED_GEMINI_MODEL") or "gemini-flash-latest",
            api_key=key,
            base_url=_env("MANAGED_GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta",
        )

    if key := _env("MANAGED_OPENAI_API_KEY"):
        configs["openai"] = RuntimeLlmConfig(
            model=_env("MANAGED_OPENAI_MODEL") or "gpt-5.4",
            api_key=key,
            base_url=_env("MANAGED_OPENAI_BASE_URL") or "https://api.openai.com/v1",
        )

    if key := _env("MANAGED_CLAUDE_API_KEY"):
        configs["anthropic"] = RuntimeLlmConfig(
            model=_env("MANAGED_CLAUDE_MODEL") or "claude-sonnet-4-6",
            api_key=key,
            base_url=_env("MANAGED_CLAUDE_BASE_URL") or "https://api.anthropic.com/v1",
        )

    return configs


def _env(name: str) -> str:
    import os

    return os.getenv(name, "").strip()


def select_questions(all_questions: list[dict[str, Any]], question_ids: list[str]) -> list[dict[str, Any]]:
    if not question_ids:
        return all_questions
    wanted = set(question_ids)
    return [item for item in all_questions if item.get("id") in wanted]


def answer_preview(answer: str, *, limit: int = 240) -> str:
    compact = " ".join(answer.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def run_matrix(
    *,
    questions: list[dict[str, Any]],
    provider_configs: dict[str, RuntimeLlmConfig],
    provider_order: list[str],
    max_iterations: int,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    results: list[dict[str, Any]] = []

    for question in questions:
        question_id = question["id"]
        prompt = question["question"]
        family = question.get("family")
        notes = question.get("notes")

        print(f"\n## {question_id} | {prompt}", flush=True)
        for provider in provider_order:
            config = provider_configs[provider]
            print(f"START provider={provider} model={config.model}", flush=True)

            entry: dict[str, Any] = {
                "question_id": question_id,
                "family": family,
                "question": prompt,
                "notes": notes,
                "provider": provider,
                "model": config.model,
            }
            try:
                result = run_agent_request(
                    RuntimeAgentRequest(
                        question=prompt,
                        llm_config=config,
                        max_iterations=max_iterations,
                    )
                )
                dataset_rows = len(result.dataset.rows) if result.dataset else None
                entry.update(
                    {
                        "status": result.status,
                        "mode": result.decision.mode,
                        "elapsed_ms": result.elapsedMs,
                        "loop_count": result.metrics.loopCount,
                        "tool_calls": result.metrics.toolCallCount,
                        "sql_calls": result.metrics.sqlCallCount,
                        "news_calls": result.metrics.newsCallCount,
                        "skill_loads": result.metrics.skillLoadCount,
                        "error_count": result.metrics.errorCount,
                        "sql_used": bool(result.sql),
                        "dataset_rows": dataset_rows,
                        "answer_preview": answer_preview(result.decision.assistantMessage or ""),
                    }
                )
                print(
                    "RESULT "
                    f"provider={provider} status={result.status} mode={result.decision.mode} "
                    f"elapsed_ms={result.elapsedMs} loops={result.metrics.loopCount} "
                    f"tools={result.metrics.toolCallCount} skills={result.metrics.skillLoadCount} "
                    f"sql_calls={result.metrics.sqlCallCount} sql_used={bool(result.sql)} dataset_rows={dataset_rows}",
                    flush=True,
                )
            except Exception as exc:
                entry.update(
                    {
                        "status": "exception",
                        "mode": None,
                        "elapsed_ms": None,
                        "loop_count": None,
                        "tool_calls": None,
                        "sql_calls": None,
                        "news_calls": None,
                        "skill_loads": None,
                        "error_count": None,
                        "sql_used": None,
                        "dataset_rows": None,
                        "answer_preview": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"ERROR provider={provider} error={entry['error']}", flush=True)

            results.append(entry)

    return {
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "max_iterations": max_iterations,
        "providers": provider_order,
        "results": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Provider Evaluation",
        "",
        f"- started_at: `{report['started_at']}`",
        f"- completed_at: `{report['completed_at']}`",
        f"- max_iterations: `{report['max_iterations']}`",
        f"- providers: `{', '.join(report['providers'])}`",
        "",
        "| question_id | provider | model | status | mode | elapsed_ms | loops | tool_calls | skill_loads | sql_calls | sql_used | dataset_rows | answer_preview |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for item in report["results"]:
        lines.append(
            "| {question_id} | {provider} | {model} | {status} | {mode} | {elapsed_ms} | {loop_count} | {tool_calls} | {skill_loads} | {sql_calls} | {sql_used} | {dataset_rows} | {answer_preview} |".format(
                question_id=item.get("question_id", ""),
                provider=item.get("provider", ""),
                model=item.get("model", ""),
                status=item.get("status", ""),
                mode=item.get("mode", "") or "",
                elapsed_ms=item.get("elapsed_ms", "") if item.get("elapsed_ms") is not None else "",
                loop_count=item.get("loop_count", "") if item.get("loop_count") is not None else "",
                tool_calls=item.get("tool_calls", "") if item.get("tool_calls") is not None else "",
                skill_loads=item.get("skill_loads", "") if item.get("skill_loads") is not None else "",
                sql_calls=item.get("sql_calls", "") if item.get("sql_calls") is not None else "",
                sql_used=item.get("sql_used", "") if item.get("sql_used") is not None else "",
                dataset_rows=item.get("dataset_rows", "") if item.get("dataset_rows") is not None else "",
                answer_preview=(item.get("answer_preview", "") or item.get("error", "")).replace("|", "/"),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    load_env(REPO_ROOT / ".env")

    all_questions = load_questions(args.questions_file)
    questions = select_questions(all_questions, args.question_ids)
    if not questions:
        raise SystemExit("No questions selected.")

    provider_configs = build_provider_configs()
    if not provider_configs:
        raise SystemExit("No provider API keys configured in .env.")

    provider_order = args.providers or list(provider_configs.keys())
    missing = [provider for provider in provider_order if provider not in provider_configs]
    if missing:
        raise SystemExit(f"Missing provider config for: {', '.join(missing)}")

    report = run_matrix(
        questions=questions,
        provider_configs=provider_configs,
        provider_order=provider_order,
        max_iterations=args.max_iterations,
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = DEFAULT_RESULTS_DIR / f"{args.output_prefix}_{timestamp}.json"
    md_path = DEFAULT_RESULTS_DIR / f"{args.output_prefix}_{timestamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
