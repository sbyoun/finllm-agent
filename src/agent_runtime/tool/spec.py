from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolSpec:
    name: str
    params: dict[str, Any] = field(default_factory=dict)
