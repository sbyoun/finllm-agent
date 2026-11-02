import io
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.modules.setdefault("oracledb", types.SimpleNamespace(connect=lambda **_: None))

from agent_runtime.llm import gemini as gemini_mod
from agent_runtime.llm.client import LLMResponse, LLMUsage, Message
from agent_runtime.llm.gemini import GeminiClient
from agent_runtime.llm.metering import MeteredLLMClient, build_usage_row, record_usage


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Seq:
    def __init__(self, responses):
        self.responses = list(responses)

    def completion(self, messages, tools=None):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(**usage):
    return LLMResponse(message=Message(role="assistant", content="x"), usage=LLMUsage(**usage) if usage else None)


class GeminiUsageParseTest(unittest.TestCase):
    def test_parses_usage_metadata_and_model_version(self):
        body = {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 1200,
                "candidatesTokenCount": 80,
                "thoughtsTokenCount": 300,
                "cachedContentTokenCount": 1000,
            },
            "modelVersion": "gemini-3-flash",
        }
        with mock.patch.object(gemini_mod, "urlopen", return_value=_FakeResponse(json.dumps(body).encode())):
            out = GeminiClient(model="gemini-flash-latest", api_key="k").completion([{"role": "user", "content": "q"}])
        self.assertEqual(out.usage.input_tokens, 1200)
        self.assertEqual(out.usage.output_tokens, 80)
        self.assertEqual(out.usage.thinking_tokens, 300)
        self.assertEqual(out.usage.cached_tokens, 1000)
        self.assertEqual(out.usage.model_version, "gemini-3-flash")


class MeteredClientTest(unittest.TestCase):
    def test_sums_calls_and_counts_failures(self):
        inner = _Seq([
            _resp(input_tokens=100, output_tokens=10, thinking_tokens=5, model_version="v1"),
            _resp(),  # 공급자가 usage 를 안 준 호출
            RuntimeError("boom"),
            _resp(input_tokens=50, output_tokens=1, cached_tokens=40, model_version="v1"),
        ])
        m = MeteredLLMClient(inner, provider="gemini", model="gemini-flash-latest")
        m.completion([])
        m.completion([])
        with self.assertRaises(RuntimeError):
            m.completion([])
        m.completion([])
        s = m.summary()
        self.assertEqual((s["calls"], s["callsWithoutUsage"], s["failedCalls"]), (3, 1, 1))
        self.assertEqual((s["inputTokens"], s["outputTokens"], s["thinkingTokens"], s["cachedTokens"]), (150, 11, 5, 40))
        row = build_usage_row(run_id="r", summary=s, user_id=None, session_id=None, source="chat",
                              billing="managed", plan_code="guest", status="ok", elapsed_ms=1, loop_count=2)
        self.assertEqual(row["model_version"], "v1")
        self.assertEqual(row["source"], "chat")


class RecordUsageFallbackTest(unittest.TestCase):
    def test_writes_local_ledger_when_supabase_env_missing(self):
        with TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"NEXT_PUBLIC_SUPABASE_URL": "", "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""}):
            record_usage({"run_id": "r1", "calls": 1}, repo_root=Path(tmp))
            lines = (Path(tmp) / "logs" / "llm_usage.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["run_id"], "r1")


class RunRecordsUsageTest(unittest.TestCase):
    def test_run_records_usage_even_when_loop_raises(self):
        from agent_runtime import service

        recorded = []
        inner = _Seq([_resp(input_tokens=10, output_tokens=2)])
        fake_agent = types.SimpleNamespace(llm=MeteredLLMClient(inner, provider="gemini", model="m"))

        def boom(*a, **k):
            fake_agent.llm.completion([])
            raise RuntimeError("loop failed")

        with TemporaryDirectory() as tmp, \
                mock.patch.object(service, "_build_agent", return_value=fake_agent), \
                mock.patch.object(service, "_run_agent_loop", side_effect=boom), \
                mock.patch.object(service, "record_usage", side_effect=lambda row, repo_root: recorded.append(row)):
            req = service.RuntimeAgentRequest(question="q", user_id="u", usage_source="telegram")
            with self.assertRaises(RuntimeError):
                service.run_agent_request(req, repo_root=tmp)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["status"], "error")
        self.assertEqual(recorded[0]["input_tokens"], 10)
        self.assertEqual(recorded[0]["billing"], "managed")
        self.assertEqual(recorded[0]["source"], "telegram")


if __name__ == "__main__":
    unittest.main()
