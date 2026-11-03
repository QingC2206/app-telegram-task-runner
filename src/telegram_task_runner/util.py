from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import tempfile
import time


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def redact(value, secret=''):
    text = str(value)
    if secret:
        text = text.replace(secret, '[redacted]')
    return re.sub(r'(?:bot)?\d{5,}:[A-Za-z0-9_-]{15,}', '[redacted]', text)


def contained(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('路径超出允许目录：' + relative)
    return path


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def runtime(self):
        return self.root / 'runtime'

    @property
    def plugins(self):
        return self.root / 'plugins'

    @property
    def config(self):
        return self.runtime / 'robot.json'

    def ensure(self):
        for path in (self.runtime, self.plugins, self.runtime / 'jobs', self.runtime / 'plugin-state'):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    def log(self, message, secret=''):
        path = self.runtime / 'runner.log'
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            os.replace(path, path.with_suffix('.log.old'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(time.strftime('%Y-%m-%d %H:%M:%S ') + redact(message, secret) + '\n')
