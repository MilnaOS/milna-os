"""
Mode controller — potato / economy / standard / blade.
Mode determines isolates-per-leg. Auto-detects hardware but user always wins.
Blade mode requires a corporate license — enforced at set() time.
"""
import subprocess
from dataclasses import dataclass


@dataclass
class Mode:
    name: str
    isolates_per_leg: int
    max_rounds: int
    description: str


MODES: dict[str, Mode] = {
    'potato':   Mode('potato',   1, 1, 'One isolate per leg - minimal spend, any hardware'),
    'economy':  Mode('economy',  2, 2, 'Two isolates per leg - balanced cost'),
    'standard': Mode('standard', 3, 4, 'Three isolates per leg - solid quality'),
    'blade':    Mode('blade',    8, 8, 'Maximum isolates - for high-end rigs'),
}


def detect_gpu_count() -> int:
    """Best-effort GPU count via nvidia-smi. Returns 0 if unavailable."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return len(lines)
    except Exception:
        return 0


def detect_vram_mb() -> int:
    """Total VRAM across all GPUs in MB. Returns 0 if unavailable."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5,
        )
        values = [int(l.strip()) for l in result.stdout.strip().splitlines() if l.strip()]
        return sum(values)
    except Exception:
        return 0


def suggest_mode() -> str:
    """Suggest a mode based on detected hardware."""
    gpus  = detect_gpu_count()
    vram  = detect_vram_mb()

    if gpus >= 8:
        return 'blade'
    if gpus >= 2 or vram >= 8000:
        return 'standard'
    if gpus == 1 and vram >= 4000:
        return 'economy'
    return 'potato'


class ModeController:
    def __init__(self, mode_name: str | None = None, license=None):
        from .license import License
        self.license   = license or License()
        suggested      = suggest_mode()
        # Never auto-suggest blade — hardware detection doesn't imply license
        if suggested == 'blade':
            suggested = 'standard'
        requested      = self.license.enforce_mode(mode_name or suggested)
        self._mode     = MODES.get(requested, MODES[suggested])
        self.suggested = suggested

    @property
    def mode(self) -> Mode:
        return self._mode

    def set(self, name: str) -> None:
        if name not in MODES:
            raise ValueError(f'Unknown mode: {name}. Choose from: {list(MODES)}')
        permitted = self.license.enforce_mode(name)
        if permitted != name:
            raise PermissionError(
                f'Blade mode requires a corporate license. '
                f'Contact sales@aimstudio.app to upgrade.'
            )
        self._mode = MODES[name]

    def isolates_for(self, leg_count: int) -> int:
        return self._mode.isolates_per_leg * leg_count

    def summary(self) -> str:
        m = self._mode
        return (
            f'{m.name.upper()} — {m.description} '
            f'({m.isolates_per_leg} isolate(s)/leg, max {m.max_rounds} round(s))'
        )
