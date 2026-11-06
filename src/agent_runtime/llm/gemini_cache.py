"""Gemini 명시 컨텍스트 캐시 — 시스템 프롬프트·툴 정의·스키마 안내(호출당 약 1만 700토큰)를 캐시에 올려 둔다.

11/6 측정: 자동(implicit) 캐시는 요청이 처음부터 끝까지 완전히 같을 때만 적중했다(끝에 '.' 하나만 달라도 0).
질문이 매번 다른 서비스에서는 사실상 쓸 수 없어, 고정 부분을 cachedContents 로 만들어 참조한다
(같은 측정에서 질문 4개 모두 10,744/10,75x 토큰 적중). 캐시 읽기는 입력 단가의 1/10, 대신 보관비(시간당)가 붙는다.

- 우리 관리형 키(MANAGED_GEMINI_API_KEY)에만 쓴다 — BYOK 사용자의 키에 보관비를 지우지 않는다.
- 내용(모델·시스템·툴) 해시로 찾고, 없으면 1시간짜리로 만든다. 남은 시간이 30분 아래면 쓰는 김에 1시간으로 연장.
  안 쓰는 시간엔 만료돼 보관비가 멈춘다. 날짜가 바뀌면 시스템 프롬프트가 바뀌어 새 캐시가 생긴다.
- GEMINI_EXPLICIT_CACHE=off 로 끈다. 실패하면 호출자가 캐시 없이 보낸다(gemini.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
TTL_SECONDS = 3600
EXTEND_BELOW_SECONDS = 1800
_lock = threading.Lock()
_entries: dict[str, tuple[str, float]] = {}  # key -> (cache name, 만료 시각 epoch)


def enabled_for(api_key: str) -> bool:
    if os.getenv("GEMINI_EXPLICIT_CACHE", "on").strip().lower() == "off":
        return False
    managed = os.getenv("MANAGED_GEMINI_API_KEY", "").strip()
    return bool(managed) and api_key == managed


def _key(model: str, system_instruction: Any, tools: Any) -> str:
    raw = json.dumps([model, system_instruction, tools], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _call(method: str, url: str, body: dict | None = None) -> dict:
    req = Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                  headers={"Content-Type": "application/json"}, method=method)
    with urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def get_or_create(*, api_key: str, model: str, system_instruction: Any, tools: Any) -> str | None:
    """캐시 이름(cachedContents/…) 또는 None(만들지 못함 → 캐시 없이 보낸다)."""
    key = _key(model, system_instruction, tools)
    now = time.time()
    with _lock:
        hit = _entries.get(key)
        if hit and hit[1] - now > 60:
            name, expire = hit
            if expire - now < EXTEND_BELOW_SECONDS:
                try:
                    _call("PATCH", f"{API}/{name}?updateMask=ttl&key={api_key}", {"ttl": f"{TTL_SECONDS}s"})
                    _entries[key] = (name, now + TTL_SECONDS)
                except Exception as exc:  # noqa: BLE001 — 연장 실패면 남은 시간까지 쓰고 다음에 새로 만든다
                    logger.warning("gemini cache extend failed: %s", str(exc)[:200])
            return name
        try:
            created = _call("POST", f"{API}/cachedContents?key={api_key}", {
                "model": f"models/{model}",
                "ttl": f"{TTL_SECONDS}s",
                "systemInstruction": system_instruction,
                "tools": tools,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("gemini cache create failed: %s", str(exc)[:300])
            return None
        name = created.get("name")
        if not name:
            return None
        _entries[key] = (name, now + TTL_SECONDS)
        logger.info("gemini cache created %s tokens=%s", name, (created.get("usageMetadata") or {}).get("totalTokenCount"))
        return name


def forget(name: str) -> None:
    """서버가 캐시를 모른다고 하면(만료·모델 변경) 지워서 다음 호출이 새로 만들게 한다."""
    with _lock:
        for key, (n, _) in list(_entries.items()):
            if n == name:
                _entries.pop(key, None)
