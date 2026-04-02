from __future__ import annotations

from abc import ABC, abstractmethod


class BaseConversation(ABC):
    @abstractmethod
    def send_message(self, message: str) -> None:
        ...

    @abstractmethod
    def run(self) -> None:
        ...
