from openai import AsyncOpenAI
from .base import BaseProvider


class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str, model: str = 'gpt-4o-mini'):
        self.client = AsyncOpenAI(api_key=api_key)
        self.default_model = model

    @property
    def name(self) -> str:
        return 'openai'

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> str:
        all_messages = [{'role': 'system', 'content': system}] + messages
        response = await self.client.chat.completions.create(
            model=model or self.default_model,
            messages=all_messages,
            max_tokens=4096,
        )
        return response.choices[0].message.content

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        # OpenAI doesn't have a dedicated count endpoint — use tiktoken estimate
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model or self.default_model)
        except KeyError:
            enc = tiktoken.get_encoding('cl100k_base')
        total = len(enc.encode(system))
        for m in messages:
            total += len(enc.encode(m.get('content', '')))
        return total
