import anthropic
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
            system=system,
            messages=messages,
        )
        return response.content[0].text

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        response = await self.client.messages.count_tokens(
            model=model or self.default_model,
            system=system,
            messages=messages,
        )
        return response.input_tokens
