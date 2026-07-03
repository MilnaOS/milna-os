"""
Session logger — audit trail + full turn logging to disk.
Metadata goes to a daily .jsonl file.
Prompts and responses go to session_logs/<session_id>/turns/.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path


class SessionLogger:
    def __init__(self, log_dir: str = 'session_logs', license_id: str = 'trial'):
        self.log_dir    = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.license_id = license_id
        self.session_id = datetime.utcnow().strftime('%Y%m%d_%H%M%S_') + str(uuid.uuid4())[:6]
        self.turn_count = 0

        # Per-session folder for turn transcripts
        self.session_dir = self.log_dir / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / 'turns').mkdir(exist_ok=True)

        self._log('session_start')

    def _log(self, event: str, meta: dict | None = None) -> None:
        entry = {
            'ts':      datetime.utcnow().isoformat(),
            'license': self.license_id,
            'session': self.session_id,
            'event':   event,
        }
        if meta:
            entry.update(meta)
        log_file = self.log_dir / f'{datetime.utcnow().strftime("%Y-%m-%d")}.jsonl'
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')

    def log_turn(
        self,
        user_message: str,
        response: str,
        legs_used: list[str],
        dead_legs: list[str],
        deep: bool,
        elapsed_ms: int = 0,
    ) -> None:
        self.turn_count += 1
        n = f'{self.turn_count:03d}'
        turns_dir = self.session_dir / 'turns'

        # Write prompt and response as human-readable markdown
        (turns_dir / f'{n}_user.md').write_text(user_message, encoding='utf-8')
        (turns_dir / f'{n}_milna.md').write_text(response, encoding='utf-8')

        self._log('turn', {
            'turn':    self.turn_count,
            'legs':    legs_used,
            'dead':    dead_legs,
            'deep':    deep,
            'ms':      elapsed_ms,
        })

    def log_end(self) -> None:
        self._log('session_end', {'turns': self.turn_count})
        # Write a session summary index
        summary = {
            'session_id': self.session_id,
            'turns':      self.turn_count,
            'ended_at':   datetime.utcnow().isoformat(),
        }
        (self.session_dir / 'session.json').write_text(
            json.dumps(summary, indent=2), encoding='utf-8')
