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
from .providers.groq_provider import GroqProvider
from .providers.generic_provider import GenericProvider
from .license import License

SERVICE = 'milna_os'

PROVIDER_DEFAULTS = {
    'anthropic':  'claude-haiku-4-5-20251001',
    'openai':     'gpt-4o-mini',
    'gemini':     'gemini-2.5-flash',
    'groq':       'llama-3.1-8b-instant',
    'ollama':     'mistral',
    # Free OpenAI-compatible providers
    'cerebras':   'llama3.1-8b',
    'openrouter': 'nvidia/nemotron-nano-9b-v2:free',
    'together':   'meta-llama/Llama-3.3-70B-Instruct-Turbo',
    'sambanova':  'Meta-Llama-3.3-70B-Instruct',
    'mistral':    'mistral-small-latest',
}

# Base URLs for OpenAI-compatible providers
_PROVIDER_URLS = {
    'cerebras':   'https://api.cerebras.ai/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
    'together':   'https://api.together.xyz/v1',
    'sambanova':  'https://api.sambanova.ai/v1',
    'mistral':    'https://api.mistral.ai/v1',
}

# Conservative request gaps (seconds) — stays within free-tier rate limits
_PROVIDER_GAPS = {
    'cerebras':   2.0,   # 30 RPM free tier
    'openrouter': 1.0,   # polite default; free models have no hard limit
    'together':   1.5,   # generous free credit
    'sambanova':  2.0,   # 30 RPM free tier
    'mistral':    1.5,   # free tier on La Plateforme
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
        return [p for p in PROVIDER_DEFAULTS if self.get_key(p)]


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

    def __init__(self, inventory: KeyInventory, ollama_url: str = 'http://localhost:11434',
                 license: License | None = None):
        self.inventory     = inventory
        self.ollama_url    = ollama_url
        self.license       = license or License()
        self.legs:          dict[str, Leg] = {}
        self.ollama_models: list[str]      = []
        self.benched:       set[str]       = set()  # models excluded from pipeline

    def build(self, model_overrides: dict[str, str] | None = None) -> list[Leg]:
        overrides = model_overrides or {}
        self.legs = {}

        # Cloud legs — one per stored key, capped by license tier.
        # Filter to providers that actually have keys before applying the cap so
        # position in the list doesn't exclude a provider that is the only key stored.
        native = ['anthropic', 'openai', 'gemini', 'groq']
        generic = ['cerebras', 'openrouter', 'together', 'sambanova', 'mistral']
        providers_with_keys = [p for p in (native + generic) if self.inventory.get_key(p)]
        capped = providers_with_keys  # no cap on dev machine
        for provider_name in capped:
            key = self.inventory.get_key(provider_name)
            model = overrides.get(provider_name, PROVIDER_DEFAULTS[provider_name])
            provider = self._make_provider(provider_name, key, model)
            self.legs[provider_name] = Leg(
                name=provider_name, provider=provider, model=model)

        # Ollama — probe first; only add if actually running.
        # If multiple models are installed, create one leg per model (up to 3)
        # so parallel extraction gets genuinely diverse signal.
        self.ollama_models = probe_ollama(self.ollama_url)
        if self.ollama_models:
            active_models = self.ollama_models[:3]
            for i, ollama_model in enumerate(active_models):
                leg_name = 'ollama' if i == 0 else f'ollama_{i}'
                self.legs[leg_name] = Leg(
                    name=leg_name,
                    provider=OllamaProvider(base_url=self.ollama_url, model=ollama_model),
                    model=ollama_model,
                )

        return self.license.apply_leg_cap(self.active_legs())

    def _make_provider(self, name: str, key: str, model: str) -> BaseProvider:
        if name == 'anthropic':
            return AnthropicProvider(api_key=key, model=model)
        if name == 'openai':
            return OpenAIProvider(api_key=key, model=model)
        if name == 'gemini':
            return GeminiProvider(api_key=key, model=model)
        if name == 'groq':
            return GroqProvider(api_key=key, model=model)
        if name in _PROVIDER_URLS:
            extra = {'HTTP-Referer': 'https://aimstudio.app', 'X-Title': 'Kilonova'} if name == 'openrouter' else None
            return GenericProvider(
                api_key=key,
                base_url=_PROVIDER_URLS[name],
                model=model,
                name=name,
                min_gap=_PROVIDER_GAPS.get(name, 1.5),
                extra_headers=extra,
            )
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
