from __future__ import annotations
from dataclasses import dataclass
import hashlib
from pathlib import Path
import json
import os
import re
import tempfile
import time
import zipfile


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


def contained(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('路径超出允许目录：' + str(relative))
    return path


def sanitize_filename(name: str, default: str = 'attachment.bin') -> str:
    raw = str(name or '').replace('\\', '/').split('/')[-1].strip()
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', '_', raw).strip('. ')
    if not cleaned:
        return default
    if len(cleaned) > 120:
        stem, suffix = os.path.splitext(cleaned)
        cleaned = stem[:max(1, 120 - len(suffix))] + suffix[:32]
    return cleaned


def file_hashes(path: Path) -> dict:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
    return {'size': size, 'md5': md5.hexdigest(), 'sha256': sha256.hexdigest()}


def split_archive(path: Path, out_dir: Path, part_size_bytes: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / (path.name + '.zip')
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=path.name)
    if archive_path.stat().st_size <= part_size_bytes:
        return [archive_path]
    parts = []
    with archive_path.open('rb') as src:
        index = 1
        while chunk := src.read(part_size_bytes):
            part = out_dir / f'{path.name}.zip.{index:03d}'
            part.write_bytes(chunk)
            parts.append(part)
            index += 1
    archive_path.unlink(missing_ok=True)
    return parts



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
