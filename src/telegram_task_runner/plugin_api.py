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

    @property
    def inputs_dir(self) -> Path:
        path = self.job_dir / 'inputs'
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def outputs_dir(self) -> Path:
        path = self.job_dir / 'outputs'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def progress(self, message, stage='running'):
        with (self.job_dir / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': time.time(), 'stage': stage, 'message': str(message)[:1000]}, ensure_ascii=False) + '\n')

    def load_state(self):
        path = self.state_dir / 'state.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def save_state(self, value):
        atomic_json(self.state_dir / 'state.json', value)


def document(path: str | Path, *, name: str | None = None, caption: str = '') -> dict:
    item = {'path': str(path), 'kind': 'document'}
    if name:
        item['name'] = str(name)
    if caption:
        item['caption'] = str(caption)[:900]
    return item


def photo(path: str | Path, *, name: str | None = None, caption: str = '') -> dict:
    item = {'path': str(path), 'kind': 'photo'}
    if name:
        item['name'] = str(name)
    if caption:
        item['caption'] = str(caption)[:900]
    return item


def result(message, *, ok=True, data=None, status=None, files=None):
    payload = {
        'ok': bool(ok),
        'message': str(message),
        'data': data or {},
        'status': status or ('succeeded' if ok else 'failed'),
    }
    if files:
        normalized = []
        for item in files:
            if isinstance(item, (str, Path)):
                normalized.append({'path': str(item), 'kind': 'document'})
            elif isinstance(item, dict) and item.get('path'):
                entry = {'path': str(item['path']), 'kind': item.get('kind', 'document')}
                if item.get('name'):
                    entry['name'] = str(item['name'])
                if item.get('caption'):
                    entry['caption'] = str(item['caption'])[:900]
                normalized.append(entry)
            else:
                raise ValueError('无效的回传文件项')
        payload['files'] = normalized
    return payload

