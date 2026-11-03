"""Trusted local plugin API v1. No Telegram token or direct polling is provided."""
from dataclasses import dataclass
from pathlib import Path
import json
import time
from .util import atomic_json


@dataclass
class Context:
    plugin_dir: Path
    state_dir: Path
    job_dir: Path
    config: dict
    job_id: str

    def progress(self, message, stage='running'):
        with (self.job_dir / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': time.time(), 'stage': stage, 'message': str(message)[:1000]}, ensure_ascii=False) + '\n')

    def load_state(self):
        path = self.state_dir / 'state.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def save_state(self, value):
        atomic_json(self.state_dir / 'state.json', value)


def result(message, *, ok=True, data=None, status=None):
    return {'ok': bool(ok), 'message': str(message), 'data': data or {}, 'status': status or ('succeeded' if ok else 'failed')}
