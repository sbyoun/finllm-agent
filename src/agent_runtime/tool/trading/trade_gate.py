"""Trade gate: provenance-style guard that prevents `place_trade` from buying
symbols that were not (a) named in the current user message, (b) listed in the
prior assistant message, or (c) discovered by tools within the current run.

Computed once per request in service.py and stashed into agent_state under
``trade_gate``. ``place_trade._execute`` reads it before any DB write.
"""

from __future__ import annotations

import re
from typing import Iterable

# Korean ticker = 6 digits. We accept any 6-digit run as a candidate.
_TICKER_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

# User-intent escape hatch: phrases that mean "ignore prior context, do a
# fresh analysis right now".
_NEW_INTENT_RE = re.compile(
    r"(새로|다시|새롭게|다른\s*종목|새\s*추천|새로운|새\s*포트폴리오\s*에\s*맞게|처음부터)"
)


_NAME_TO_TICKER: dict[str, str] | None = None


def _load_name_map() -> dict[str, str]:
    global _NAME_TO_TICKER
    if _NAME_TO_TICKER is not None:
        return _NAME_TO_TICKER
    mapping: dict[str, str] = {}
    try:
        from agent_runtime.tool.sql.oracle import OracleSQLRunner

        runner = OracleSQLRunner()
        _, rows = runner("SELECT ticker, name FROM stocks WHERE country = 'KR'")
        for row in rows:
            ticker = str(row.get("ticker") or "").strip()
            name = str(row.get("name") or "").strip()
            if ticker and name:
                mapping[name] = ticker
    except Exception:
        pass
    _NAME_TO_TICKER = mapping
    return mapping


def _extract_symbols(text: str) -> set[str]:
    if not text:
        return set()
    found: set[str] = set()
    for match in _TICKER_RE.findall(text):
        found.add(match)
    name_map = _load_name_map()
    if name_map:
        # Sort by length desc so longer names match first (avoids partial matches).
        for name in sorted(name_map.keys(), key=len, reverse=True):
            if len(name) >= 2 and name in text:
                found.add(name_map[name])
    return found


def has_new_intent(question: str) -> bool:
    return bool(_NEW_INTENT_RE.search(question or ""))


def compute_trade_gate(
    *,
    question: str,
    history: Iterable[object],  # list of objects with .role/.content
    prior_assistant_text: str = "",
) -> dict:
    """Return gate dict.

    Shape:
        {
            "allowed_symbols": list[str] | None,   # None = unconstrained
            "source": "explicit" | "prior" | "explicit+prior" | "free",
        }
    """
    explicit = _extract_symbols(question)
    new_intent = has_new_intent(question)

    prior: set[str] = set()
    if not new_intent:
        # Walk history backward to find the most recent assistant message with
        # any symbol mention.
        items = list(history)
        for item in reversed(items):
            role = getattr(item, "role", None)
            content = getattr(item, "content", "") or ""
            if role == "assistant":
                prior = _extract_symbols(content)
                if prior:
                    break
        if not prior and prior_assistant_text:
            prior = _extract_symbols(prior_assistant_text)

    allowed = explicit | prior
    if not allowed:
        return {"allowed_symbols": None, "source": "free"}
    if explicit and prior:
        source = "explicit+prior"
    elif explicit:
        source = "explicit"
    else:
        source = "prior"
    return {"allowed_symbols": sorted(allowed), "source": source}
