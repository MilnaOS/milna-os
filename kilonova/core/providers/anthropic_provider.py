import anthropic
from typing import AsyncIterator
from .base import BaseProvider


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str, model: str = 'claude-haiku-4-5'):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.default_model = model

    @property
    def name(self) -> str:
        return 'anthropic'

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> str:
        response = await self.client.messages.create(
            model=model or self.default_model,
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        return response.content[0].text

    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=model or self.default_model,
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        ) as s:
            async for chunk in s.text_stream:
                yield chunk

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        response = await self.client.messages.count_tokens(
            model=model or self.default_model,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        return response.input_tokens
