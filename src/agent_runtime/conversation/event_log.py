from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from agent_runtime.event.base import Event


class EventLog:
    def __init__(self, persistence_dir: str | None = None) -> None:
        self._events: list[Event] = []
        self._persistence_path: Path | None = None
        if persistence_dir:
            base = Path(persistence_dir)
            base.mkdir(parents=True, exist_ok=True)
            self._persistence_path = base / "events.jsonl"

    def append(self, event: Event) -> None:
        self._events.append(event)
        if self._persistence_path is not None:
            with self._persistence_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __getitem__(self, index):
        return self._events[index]
