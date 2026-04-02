from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_runtime.context.view import View
from agent_runtime.event.condensation import CondensationEvent


class CondenserBase(Protocol):
    def condense(self, view: View) -> CondensationEvent | None:
        ...


@dataclass(slots=True)
class NoOpCondenser:
    def condense(self, view: View) -> CondensationEvent | None:  # noqa: ARG002
        return None
