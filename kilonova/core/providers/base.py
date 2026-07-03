from abc import ABC, abstractmethod


class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> str:
        pass

    @abstractmethod
    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
