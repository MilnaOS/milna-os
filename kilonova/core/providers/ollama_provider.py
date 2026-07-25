import json as _json
import httpx
from typing import AsyncIterator
from .base import BaseProvider, normalize_messages


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str = 'http://localhost:11434', model: str = 'mistral'):
        self.base_url = base_url.rstrip('/')
        self.default_model = model

    @property
    def name(self) -> str:
        return 'ollama'

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
    ) -> str:
        all_messages = [{'role': 'system', 'content': system}] + normalize_messages(messages)
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f'{self.base_url}/api/chat',
                json={
                    'model': model or self.default_model,
                    'messages': all_messages,
                    'stream': False,
                },
            )
            response.raise_for_status()
            return response.json()['message']['content']

    async def stream(self, model: str, system: str, messages: list[dict]) -> AsyncIterator[str]:
        all_messages = [{'role': 'system', 'content': system}] + normalize_messages(messages)
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                'POST',
                f'{self.base_url}/api/chat',
                json={
                    'model': model or self.default_model,
                    'messages': all_messages,
                    'stream': True,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    obj = _json.loads(line)
                    content = obj.get('message', {}).get('content', '')
                    if content:
                        yield content
                    if obj.get('done'):
                        break

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        # Rough estimate — Ollama has no token count endpoint
        def _content_len(c) -> int:
            if isinstance(c, str):
                return len(c)
            if isinstance(c, list):
                return sum(len(b.get('text', '')) for b in c if isinstance(b, dict))
            return 0
        total_chars = len(system) + sum(_content_len(m.get('content', '')) for m in messages)
        return total_chars // 4
