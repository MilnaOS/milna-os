import asyncio
import time
from typing import AsyncIterator
from openai import AsyncOpenAI
from .base import BaseProvider, normalize_messages

# llama-3.1-8b-instant free tier: 6000 RPM / 500K TPM.
# 0.2s gap = ~300 req/min — well within limits, prevents burst hammering.
_GROQ_MIN_GAP = 0.2   # seconds between completions


class GroqProvider(BaseProvider):
    def __init__(self, api_key: str, model: str = 'llama-3.1-8b-instant'):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url='https://api.groq.com/openai/v1',
        )
        self.default_model = model
        self._lock        = asyncio.Lock()
        self._last_call   = 0.0   # epoch seconds of last completed call

    @property
    def name(self) -> str:
        return 'groq'

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> str:
        async with self._lock:
            # Enforce minimum gap — sleep only the remaining wait, if any
            elapsed = time.monotonic() - self._last_call
            if elapsed < _GROQ_MIN_GAP:
                await asyncio.sleep(_GROQ_MIN_GAP - elapsed)

            all_messages = [{'role': 'system', 'content': system}] + normalize_messages(messages)
            response = await self.client.chat.completions.create(
                model=model or self.default_model,
                messages=all_messages,
                max_tokens=4096,
            )
            self._last_call = time.monotonic()
            return response.choices[0].message.content

    async def stream(self, model: str, system: str, messages: list[dict]) -> AsyncIterator[str]:
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < _GROQ_MIN_GAP:
                await asyncio.sleep(_GROQ_MIN_GAP - elapsed)
            all_messages = [{'role': 'system', 'content': system}] + normalize_messages(messages)
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
            self._last_call = time.monotonic()

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model or self.default_model)
        except KeyError:
            enc = tiktoken.get_encoding('cl100k_base')
        total = len(enc.encode(system))
        for m in messages:
            total += len(enc.encode(m.get('content', '')))
        return total
