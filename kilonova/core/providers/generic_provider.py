"""
generic_provider.py — OpenAI-compatible provider for any endpoint that speaks the
OpenAI Chat Completions API. Used for Cerebras, OpenRouter, Together AI,
SambaNova, Mistral, and any future OpenAI-compatible service.
"""
import asyncio
import time
from typing import AsyncIterator
from openai import AsyncOpenAI
from .base import BaseProvider, normalize_messages


class GenericProvider(BaseProvider):
    """
    Drop-in provider for any OpenAI-compatible endpoint.

    Args:
        api_key:   Provider API key
        base_url:  API root (e.g. 'https://api.cerebras.ai/v1')
        model:     Default model to use
        name:      Human-readable provider name for logging
        min_gap:   Minimum seconds between completions (rate-limit guard)
        extra_headers: Any extra HTTP headers required (e.g. OpenRouter needs HTTP-Referer)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        name: str,
        min_gap: float = 1.0,
        extra_headers: dict | None = None,
    ):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers or {},
        )
        self.default_model = model
        self._name         = name
        self._min_gap      = min_gap
        self._lock         = asyncio.Lock()
        self._last_call    = 0.0

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, model: str, system: str, messages: list[dict]) -> str:
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_gap:
                await asyncio.sleep(self._min_gap - elapsed)

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
            if elapsed < self._min_gap:
                await asyncio.sleep(self._min_gap - elapsed)
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
        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model(model or self.default_model)
            except KeyError:
                enc = tiktoken.get_encoding('cl100k_base')
            total = len(enc.encode(system))
            for m in messages:
                total += len(enc.encode(m.get('content', '')))
            return total
        except ImportError:
            # tiktoken not available — rough estimate
            return sum(len((m.get('content', '') + system).split()) for m in messages)
