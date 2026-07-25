from abc import ABC, abstractmethod
from typing import AsyncIterator


def normalize_messages(messages: list[dict]) -> list[dict]:
    """
    Strip Anthropic-only fields and flatten multi-part content blocks to plain strings.

    Anthropic's SDK injects 'cache_control' into content blocks for prompt caching.
    OpenAI-compatible APIs (Groq, Ollama, Cerebras, etc.) reject this field.
    This helper is safe to call on every message list before sending to any non-Anthropic API.
    """
    result = []
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, list):
            parts = [
                block.get('text', '')
                for block in content
                if isinstance(block, dict) and block.get('type') == 'text'
            ]
            result.append({**msg, 'content': '\n'.join(parts)})
        else:
            result.append(msg)
    return result


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
    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> AsyncIterator[str]:
        """Yield text chunks as they arrive. Must be an async generator."""
        # Default: fall back to complete() so subclasses can opt in incrementally.
        yield await self.complete(model, system, messages)

    @abstractmethod
    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
