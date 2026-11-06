import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError

from agent_runtime.llm import gemini as gm
from agent_runtime.llm import gemini_cache
from agent_runtime.llm.gemini import GeminiClient

OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 90}}
MSGS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "q"}]
TOOLS = [{"type": "function", "function": {"name": "run_sql", "description": "d", "parameters": {"type": "object", "properties": {}}}}]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GeminiExplicitCacheTest(unittest.TestCase):
    def setUp(self):
        gemini_cache._entries.clear()
        self.env = mock.patch.dict(os.environ, {"MANAGED_GEMINI_API_KEY": "managed", "GEMINI_EXPLICIT_CACHE": "on"})
        self.env.start()
        self.sent = []

    def tearDown(self):
        self.env.stop()

    def _fake(self, fail_cached_once=False):
        state = {"failed": False}

        def urlopen(req, timeout=None):
            url = req.full_url
            body = json.loads(req.data) if req.data else None
            self.sent.append((req.get_method(), url.split("?")[0].rsplit("/", 1)[-1], body))
            if url.split("?")[0].endswith("/cachedContents"):
                return _Resp(json.dumps({"name": "cachedContents/abc", "usageMetadata": {"totalTokenCount": 90}}).encode())
            if body and "cachedContent" in body and fail_cached_once and not state["failed"]:
                state["failed"] = True
                raise HTTPError(url, 403, "cache gone", {}, io.BytesIO(b'{"error":"CachedContent not found"}'))
            return _Resp(json.dumps(OK).encode())
        return urlopen

    def test_creates_once_then_reuses(self):
        with mock.patch.object(gemini_cache, "urlopen", self._fake()), mock.patch.object(gm, "urlopen", self._fake()):
            c = GeminiClient(model="gemini-flash-latest", api_key="managed")
            c.completion(MSGS, tools=TOOLS)
            r = c.completion([MSGS[0], {"role": "user", "content": "other question"}], tools=TOOLS)
        creates = [s for s in self.sent if s[1] == "cachedContents"]
        gens = [s for s in self.sent if s[1].endswith(":generateContent")]
        self.assertEqual(len(creates), 1)
        self.assertTrue(all("cachedContent" in g[2] and "tools" not in g[2] and "system_instruction" not in g[2] for g in gens))
        self.assertEqual(r.usage.cached_tokens, 90)

    def test_cache_error_falls_back_without_cache(self):
        with mock.patch.object(gemini_cache, "urlopen", self._fake()), mock.patch.object(gm, "urlopen", self._fake(fail_cached_once=True)):
            r = GeminiClient(model="gemini-flash-latest", api_key="managed").completion(MSGS, tools=TOOLS)
        gens = [s[2] for s in self.sent if s[1].endswith(":generateContent")]
        self.assertIn("cachedContent", gens[0])
        self.assertIn("system_instruction", gens[-1])  # 캐시 없이 다시
        self.assertEqual(r.message.content, "ok")
        self.assertEqual(gemini_cache._entries, {})  # 잊고 다음에 새로 만든다

    def test_byok_key_never_cached(self):
        with mock.patch.object(gemini_cache, "urlopen", self._fake()), mock.patch.object(gm, "urlopen", self._fake()):
            GeminiClient(model="gemini-flash-latest", api_key="user-own-key").completion(MSGS, tools=TOOLS)
        self.assertFalse([s for s in self.sent if s[1] == "cachedContents"])


if __name__ == "__main__":
    unittest.main()
