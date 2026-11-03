from __future__ import annotations
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from .util import Layout, atomic_json, contained
from .platforms import current

RESERVED = set('start help status plugins jobs job whoami allow revoke users'.split())


@dataclass(frozen=True)
class Plugin:
    identifier: str
    manifest: dict
    files: dict
    config: dict
    revision: str


class Registry:
    def __init__(self, layout: Layout, platform=None):
        self.layout = layout
        self.platform = platform or current()
        self.plugins, self.commands, self.errors = {}, {}, {}

    def discover(self):
        found, errors = {}, {}
        for folder in sorted(self.layout.plugins.iterdir()):
            if folder.name.startswith('.') or not folder.is_dir():
                continue
            try:
                if folder.is_symlink() or folder.resolve().parent != self.layout.plugins.resolve():
                    raise ValueError('插件目录不可链接到外部')
                manifest_path = folder / 'plugin.json'
                if not manifest_path.is_file():
                    continue
                if manifest_path.stat().st_size > 65536:
                    raise ValueError('清单过大')
                raw = manifest_path.read_bytes()
                manifest = json.loads(raw.decode('utf-8-sig'))
                if manifest.get('enabled', True) is False:
                    continue
                platforms = manifest.get('platforms', ['windows', 'macos', 'linux'])
                if not isinstance(platforms, list) or not platforms or any(p not in ('windows', 'macos', 'linux') for p in platforms):
                    raise ValueError('platforms 需要声明 windows、macos 或 linux')
                if self.platform not in platforms:
                    raise ValueError('平台不适用：此插件支持 ' + ', '.join(platforms))
                ident = manifest.get('id', '')
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', ident) or ident != folder.name:
                    raise ValueError('插件 id 必须与目录名一致，使用小写字母、数字、下划线')
                if manifest.get('api_version') != 1 or not isinstance(manifest.get('version'), str):
                    raise ValueError('需要 api_version=1 和 version 字符串')
                entry = manifest.get('entrypoint', 'main.py')
                if not re.fullmatch(r'[A-Za-z0-9_]+\.py', entry):
                    raise ValueError('入口必须为插件根目录中的 .py 文件')
                commands = manifest.get('commands')
                if not isinstance(commands, dict) or not commands:
                    raise ValueError('需要 commands 对象')
                for command, spec in commands.items():
                    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', command) or command in RESERVED:
                        raise ValueError('命令名无效或与宿主命令冲突：' + command)
                    if not isinstance(spec, dict) or not isinstance(spec.get('description'), str) or not 1 <= len(spec['description']) <= 100:
                        raise ValueError('命令需要 1–100 字符的 description')
                    if spec.get('mode') not in ('query', 'job') or spec.get('permission') not in ('view', 'execute', 'admin'):
                        raise ValueError('命令需要 mode 和 permission')
                    limit = 60 if spec['mode'] == 'query' else 7200
                    if type(spec.get('timeout_seconds', 30 if spec['mode'] == 'query' else 3600)) is not int or not 1 <= spec.get('timeout_seconds', 30 if spec['mode'] == 'query' else 3600) <= limit:
                        raise ValueError('查询超时最大 60 秒，任务超时最大 7200 秒')
                    resources = spec.get('resources', [])
                    if not isinstance(resources, list) or any(not isinstance(r, str) or not re.fullmatch(r'[a-z0-9:_-]{1,80}', r) for r in resources):
                        raise ValueError('资源锁格式无效')
                    if 'input_files' in spec:
                        in_files = spec['input_files']
                        if not isinstance(in_files, dict) or type(in_files.get('required', False)) is not bool:
                            raise ValueError('input_files 格式无效')
                        kinds = in_files.get('kinds', ['document'])
                        if not isinstance(kinds, list) or not kinds or any(k not in ('document', 'photo') for k in kinds):
                            raise ValueError('input_files.kinds 仅支持 document 或 photo')
                        exts = in_files.get('extensions', [])
                        if not isinstance(exts, list) or any(not isinstance(e, str) or not re.fullmatch(r'\.[A-Za-z0-9_+-]{1,16}', e) for e in exts):
                            raise ValueError('input_files.extensions 需以点号开头')
                        max_mb = in_files.get('max_size_mb', 2000)
                        if type(max_mb) is not int or not 1 <= max_mb <= 2000:
                            raise ValueError('input_files.max_size_mb 范围 1–2000 MB')
                    if 'output_files' in spec:
                        out_files = spec['output_files']
                        if not isinstance(out_files, dict):
                            raise ValueError('output_files 格式无效')
                        max_cnt = out_files.get('max_count', 10)
                        max_out_mb = out_files.get('max_size_mb', 2000)
                        if type(max_cnt) is not int or not 1 <= max_cnt <= 20 or type(max_out_mb) is not int or not 1 <= max_out_mb <= 2000:
                            raise ValueError('output_files 范围：max_count 1–20，max_size_mb 1–2000 MB')
                names = manifest.get('files', [entry])
                if not isinstance(names, list) or not names or len(names) > 200 or entry not in names:
                    raise ValueError('files 需要包含入口，最多 200 个文件')
                files = {'plugin.json': raw}
                for name in names:
                    if not isinstance(name, str) or any(p.startswith('.') for p in Path(name).parts):
                        raise ValueError('files 不能包含隐藏文件或 .git')
                    path = contained(folder, name)
                    if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
                        raise ValueError('插件文件缺失或过大：' + name)
                    data = path.read_bytes()
                    if name.endswith('.py'):
                        ast.parse(data, filename=name)
                    files[name] = data
                if sum(len(v) for v in files.values()) > 10 * 1024 * 1024:
                    raise ValueError('插件代码快照超过 10 MB')
                if manifest_path.read_bytes() != raw or any((folder / name).read_bytes() != data for name, data in files.items()):
                    raise ValueError('插件正在更新，请稍后重试')
                config_file = self.layout.runtime / 'plugin-state' / ident / 'config.json'
                if config_file.exists() and config_file.stat().st_size > 65536:
                    raise ValueError('插件配置超过 64 KB')
                config = json.loads(config_file.read_text(encoding='utf-8-sig')) if config_file.exists() else manifest.get('config', {})
                if not isinstance(config, dict):
                    raise ValueError('config 必须为对象')
                signature = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
                for name, data in sorted(files.items()):
                    signature.update(name.encode() + b'\0' + data)
                found[ident] = Plugin(ident, manifest, files, config, signature.hexdigest())
            except Exception as error:
                errors[folder.name] = str(error)[:300]
        owners = {}
        for plugin in found.values():
            for command in plugin.manifest['commands']:
                owners.setdefault(command, []).append(plugin.identifier)
        conflicts = {owner for values in owners.values() if len(values) > 1 for owner in values}
        for ident in conflicts:
            found.pop(ident)
            errors[ident] = '命令与其他插件冲突，双方均已停用'
        self.plugins, self.errors = found, errors
        self.commands = {name: (p, spec) for p in found.values() for name, spec in p.manifest['commands'].items()}
        return self

    def snapshot(self, plugin, job_id):
        job_dir = contained(self.layout.runtime / 'jobs', job_id)
        code = job_dir / 'code'
        code.mkdir(parents=True, exist_ok=False)
        (job_dir / 'inputs').mkdir(parents=True, exist_ok=True)
        (job_dir / 'outputs').mkdir(parents=True, exist_ok=True)
        for name, data in plugin.files.items():
            path = contained(code, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        state = self.layout.runtime / 'plugin-state' / plugin.identifier
        state.mkdir(parents=True, exist_ok=True)
        atomic_json(job_dir / 'plugin-snapshot.json', {'id': plugin.identifier, 'version': plugin.manifest['version'], 'revision': plugin.revision})
        return job_dir
