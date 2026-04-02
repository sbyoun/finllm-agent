from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass(slots=True)
class Action:
    def to_arguments_json(self) -> str:
        if is_dataclass(self):
            return json.dumps(asdict(self), ensure_ascii=False)
        return json.dumps(self.__dict__, ensure_ascii=False)


@dataclass(slots=True)
class Observation:
    content: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        return "\n".join(self.content)
