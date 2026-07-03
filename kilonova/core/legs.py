"""
Leg registry — one leg per API key. Keys live in the OS credential store
(Windows Credential Manager via keyring). Milna never writes keys to disk.
"""
import keyring
from dataclasses import dataclass, field
from .providers.base import BaseProvider
from .providers.anthropic_provider import AnthropicProvider
from .providers.openai_provider import OpenAIProvider
from .providers.ollama_provider import OllamaProvider
from .providers.gemini_provider import GeminiProvider

SERVICE = 'milna_os'

PROVIDER_DEFAULTS = {
    'anthropic': 'claude-haiku-4-5',
    'openai':    'gpt-4o-mini',
    'gemini':    'gemini-2.5-flash',
    'ollama':    'mistral',
}


@dataclass
class Leg:
    name: str               # e.g. 'anthropic', 'openai', 'ollama'
    provider: BaseProvider
    model: str
    alive: bool = True      # flips False on auth failure or provider outage
    error: str = ''


class KeyInventory:
    """Reads keys from OS keyring and builds the leg registry."""

    def store_key(self, provider: str, key: str) -> None:
        keyring.set_password(SERVICE, provider, key)

    def get_key(self, provider: str) -> str | None:
        return keyring.get_password(SERVICE, provider)

    def delete_key(self, provider: str) -> None:
        try:
            keyring.delete_password(SERVICE, provider)
        except keyring.errors.PasswordDeleteError:
            pass

    def list_stored(self) -> list[str]:
        """Returns provider names that have keys stored."""
        stored = []
        for p in PROVIDER_DEFAULTS:
            if self.get_key(p):
                stored.append(p)
        return stored


def probe_ollama(base_url: str = 'http://localhost:11434') -> list[str]:
    """
    Returns list of installed model names from Ollama, or [] if not running.
    Uses stdlib only — no async needed at startup.
    """
    import urllib.request
    import json
    try:
        with urllib.request.urlopen(f'{base_url}/api/tags', timeout=3) as r:
            data = json.loads(r.read())
            return [m['name'] for m in data.get('models', [])]
    except Exception:
        return []


class LegRegistry:
    """
    Builds and manages the set of active legs from stored keys.
    One key = one leg. Ollama is probed at startup — if running, the first
    installed model is used automatically. If not running, no Ollama leg added.
    A leg that fails is marked alive=False and dropped; others continue.
    """

    def __init__(self, inventory: KeyInventory, ollama_url: str = 'http://localhost:11434'):
        self.inventory     = inventory
        self.ollama_url    = ollama_url
        self.legs:          dict[str, Leg] = {}
        self.ollama_models: list[str]      = []
        self.benched:       set[str]       = set()  # models excluded from pipeline

    def build(self, model_overrides: dict[str, str] | None = None) -> list[Leg]:
        overrides = model_overrides or {}
        self.legs = {}

        # Cloud legs — one per stored key
        for provider_name in ['anthropic', 'openai', 'gemini']:
            key = self.inventory.get_key(provider_name)
            if not key:
                continue
            model = overrides.get(provider_name, PROVIDER_DEFAULTS[provider_name])
            provider = self._make_provider(provider_name, key, model)
            self.legs[provider_name] = Leg(
                name=provider_name, provider=provider, model=model)

        # Ollama — probe first; only add if actually running
        self.ollama_models = probe_ollama(self.ollama_url)
        if self.ollama_models:
            ollama_model = overrides.get('ollama', self.ollama_models[0])
            self.legs['ollama'] = Leg(
                name='ollama',
                provider=OllamaProvider(base_url=self.ollama_url, model=ollama_model),
                model=ollama_model,
            )

        return self.active_legs()

    def _make_provider(self, name: str, key: str, model: str) -> BaseProvider:
        if name == 'anthropic':
            return AnthropicProvider(api_key=key, model=model)
        if name == 'openai':
            return OpenAIProvider(api_key=key, model=model)
        if name == 'gemini':
            return GeminiProvider(api_key=key, model=model)
        raise ValueError(f'Unknown provider: {name}')

    def active_legs(self) -> list[Leg]:
        return [leg for leg in self.legs.values() if leg.alive]

    def diverse_pipeline_legs(self, n: int) -> list[Leg]:
        """
        Return up to N legs for pipeline mode.
        If multiple Ollama models are available, each leg uses a different model
        so isolates bring genuinely diverse signal rather than repeating one model.
        Benched models are excluded. Falls back to active_legs() if Ollama isn't
        the sole provider.
        """
        base = self.legs.get('ollama')
        if not base or not base.alive or not self.ollama_models:
            return self.active_legs()

        non_ollama = [l for l in self.active_legs() if l.name != 'ollama']
        if non_ollama:
            return self.active_legs()

        available = [m for m in self.ollama_models if m not in self.benched]
        return [
            Leg(name=f'ollama:{m}', provider=base.provider, model=m, alive=True)
            for m in available[:n]
        ]

    def toggle_bench(self, model: str) -> bool:
        """Toggle a model between active and benched. Returns True if now benched."""
        if model in self.benched:
            self.benched.discard(model)
            return False
        else:
            self.benched.add(model)
            return True

    def kill_leg(self, name: str, reason: str) -> None:
        """Mark a leg dead — called when a provider fails. Others keep running."""
        if name in self.legs:
            self.legs[name].alive = False
            self.legs[name].error = reason
