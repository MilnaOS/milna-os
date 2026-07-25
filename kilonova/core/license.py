"""
License tiers for Milna OS.

  free        — default, no key required
  individual  — $700 one-time unlock (LemonSqueezy)
  corporate   — negotiated, required for blade mode

License key stored in OS keyring under service 'milna_os', username 'license'.
Key format: MILNA-<tier>-<token>  e.g. MILNA-IND-a1b2c3d4  MILNA-CORP-x9y8z7
"""
import hashlib
import keyring

SERVICE       = 'milna_os'
_LICENSE_KEY  = 'license'

# Tier constants
FREE       = 'free'
INDIVIDUAL = 'individual'
CORPORATE  = 'corporate'

# Per-tier caps
_CAPS = {
    FREE:       {'max_keys': 3,   'max_legs': 1,    'blade': False},
    INDIVIDUAL: {'max_keys': None, 'max_legs': None, 'blade': False},
    CORPORATE:  {'max_keys': None, 'max_legs': None, 'blade': True},
}

# Known valid key prefixes — real validation would hit a license server,
# but offline prefix check is sufficient for v1.
_TIER_PREFIXES = {
    'MILNA-IND-':  INDIVIDUAL,
    'MILNA-CORP-': CORPORATE,
}


def _detect_tier(raw_key: str) -> str:
    for prefix, tier in _TIER_PREFIXES.items():
        if raw_key.upper().startswith(prefix):
            return tier
    return FREE


class License:
    def __init__(self):
        raw = keyring.get_password(SERVICE, _LICENSE_KEY)
        self.raw_key = raw or ''
        self.tier    = _detect_tier(self.raw_key) if self.raw_key else FREE
        self._caps   = _CAPS[self.tier]

    # ── Caps ─────────────────────────────────────────────────────────────

    @property
    def max_keys(self) -> int | None:
        """None = unlimited."""
        return self._caps['max_keys']

    @property
    def max_legs(self) -> int | None:
        """None = unlimited."""
        return self._caps['max_legs']

    @property
    def blade_allowed(self) -> bool:
        return self._caps['blade']

    # ── Helpers ──────────────────────────────────────────────────────────

    def apply_key_cap(self, providers: list[str]) -> list[str]:
        if self.max_keys is None:
            return providers
        return providers[:self.max_keys]

    def apply_leg_cap(self, legs: list) -> list:
        if self.max_legs is None:
            return legs
        return legs[:self.max_legs]

    def enforce_mode(self, requested_mode: str) -> str:
        """Return requested mode, or downgrade if not permitted."""
        if requested_mode == 'blade' and not self.blade_allowed:
            return 'standard'
        return requested_mode

    # ── Storage ──────────────────────────────────────────────────────────

    def store_key(self, raw_key: str) -> str:
        """Store a license key and return the detected tier."""
        tier = _detect_tier(raw_key)
        keyring.set_password(SERVICE, _LICENSE_KEY, raw_key)
        self.raw_key = raw_key
        self.tier    = tier
        self._caps   = _CAPS[tier]
        return tier

    def remove_key(self) -> None:
        try:
            keyring.delete_password(SERVICE, _LICENSE_KEY)
        except Exception:
            pass
        self.raw_key = ''
        self.tier    = FREE
        self._caps   = _CAPS[FREE]

    # ── Display ──────────────────────────────────────────────────────────

    def summary(self) -> str:
        keys = f'{self.max_keys} key slots' if self.max_keys else 'unlimited keys'
        legs = f'{self.max_legs} isolet'    if self.max_legs else 'unlimited isolets'
        blade = 'blade unlocked' if self.blade_allowed else 'blade: corporate only'
        return f'{self.tier.upper()} — {keys} · {legs} · {blade}'
