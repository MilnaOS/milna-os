import httpx
from .base import BaseProvider


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
        all_messages = [{'role': 'system', 'content': system}] + messages
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

    async def count_tokens(self, model: str, system: str, messages: list[dict]) -> int:
        # Rough estimate — Ollama has no token count endpoint
        total_chars = len(system) + sum(len(m.get('content', '')) for m in messages)
        return total_chars // 4
