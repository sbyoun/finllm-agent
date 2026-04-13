from __future__ import annotations

import json
from dataclasses import dataclass

from agent_runtime.context.view import View
from agent_runtime.event.condensation import CondensationEvent


def _estimate_view_chars(view: View) -> int:
    total = 0
    for event in view.events:
        if hasattr(event, "to_message_dict"):
            try:
                total += len(json.dumps(event.to_message_dict(), ensure_ascii=False))
            except Exception:
                total += 200
    return total


@dataclass(slots=True)
class RollingCondenser:
    max_size: int = 120
    keep_first: int = 4
    target_size: int | None = None
    max_chars: int | None = None

    def should_condense(self, view: View) -> bool:
        if len(view.events) > self.max_size:
            return True
        if self.max_chars is not None and _estimate_view_chars(view) > self.max_chars:
            return True
        return False

    def condense(self, view: View) -> CondensationEvent | None:
        if not self.should_condense(view):
            return None

        target_size = self.target_size or max(1, self.max_size // 2)
        tail_keep = max(1, target_size - self.keep_first - 1)

        if len(view.events) <= self.keep_first + tail_keep:
            return None

        summary_start = self.keep_first
        summary_end = max(summary_start, len(view.events) - tail_keep)
        middle_events = view.events[summary_start:summary_end]
        if not middle_events:
            return None

        return CondensationEvent(
            forgotten_event_ids=[event.id for event in middle_events],
            summary=self.summarize(middle_events),
            summary_offset=self.keep_first,
        )

    def summarize(self, events: list) -> str:  # pragma: no cover - abstract by convention
        raise NotImplementedError
