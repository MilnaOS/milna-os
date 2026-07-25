from typing import AsyncIterator
from openai import AsyncOpenAI
from .base import BaseProvider


class GeminiProvider(BaseProvider):
    BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'

    def __init__(self, api_key: str, model: str = 'gemini-2.5-flash'):
        self.client = AsyncOpenAI(api_key=api_key, base_url=self.BASE_URL)
        self.default_model = model

    @property
    def name(self) -> str:
        return 'gemini'

    async def complete(self, model: str, system: str, messages: list[dict]) -> str:
        all_messages = [{'role': 'system', 'content': system}] + messages
        response = await self.client.chat.completions.create(
            model=model or self.default_model,
            messages=all_messages,
            max_tokens=4096,
        )
        return response.choices[0].message.content

    async def stream(self, model: str, system: str, messages: list[dict]) -> AsyncIterator[str]:
        all_messages = [{'role': 'system', 'content': system}] + messages
        async with await self.client.chat.completions.create(
            model=model or self.default_model,
            messages=all_messages,
            max_tokens=4096,
            stream=True,
        ) as s:
            async for chunk in s:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        import tiktoken
        enc = tiktoken.get_encoding('cl100k_base')
        total = len(enc.encode(system))
        for m in messages:
            total += len(enc.encode(m.get('content', '')))
        return total
