from __future__ import annotations
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from .plugin_api import Context
from .util import Layout, atomic_json, contained, redact
from .locking import FileLock
from .platforms.processes import ProcessTree
import hashlib


def execute(layout: Layout, job: dict, stop=None, progress=None):
    spec = json.loads(job['spec']) if isinstance(job['spec'], str) else job['spec']
    with ExitStack() as locks:
        for resource in sorted(set(spec.get('resources', []))):
            name = hashlib.sha256(resource.encode()).hexdigest() + '.lock'
            locks.enter_context(FileLock(layout.runtime / 'resource-locks' / name))
        return _execute(layout, job, stop, progress)


def _execute(layout: Layout, job: dict, stop=None, progress=None):
    directory = contained(layout.runtime / 'jobs', job['id'])
    spec = json.loads(job['spec']) if isinstance(job['spec'], str) else job['spec']
    config = spec['config']
    wire = {'plugin_dir': str(directory / 'code'), 'state_dir': str(layout.runtime / 'plugin-state' / job['plugin']),
            'job_dir': str(directory), 'job_id': job['id'], 'entrypoint': spec['entrypoint'], 'config': config,
            'request': {'command': job['command'], 'argument': job['argument'], 'user_id': job['user_id'],
                        'chat_id': job['chat_id'], 'message_id': job['message_id']}}
    atomic_json(directory / 'worker.json', wire)
    keys = {'SYSTEMROOT','WINDIR','TEMP','TMP','PATH','USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)',
            'HOME','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','DOTNET_ROOT','NUGET_PACKAGES',
            'SSH_AUTH_SOCK','LANG','LC_ALL','LC_CTYPE','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'}
    env = {k:v for k,v in os.environ.items() if k.upper() in keys}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with (directory / 'worker.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-B', str(layout.root / 'runner.py'), '--root', str(layout.root), '_worker', str(directory / 'worker.json')],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=directory / 'code', env=env, **flags)
        tree = ProcessTree(process)
        started, last = time.monotonic(), ''
        try:
            while process.poll() is None:
                if stop is not None and stop.is_set():
                    raise RuntimeError('服务停止，任务被终止；请核对执行阶段后再恢复。')
                if time.monotonic() - started > spec['timeout_seconds']:
                    raise TimeoutError('任务超时，已终止本次任务进程树。')
                events = directory / 'events.jsonl'
                if progress and events.is_file():
                    with events.open('rb') as stream:
                        stream.seek(max(0, events.stat().st_size - 65536))
                        lines = stream.read().decode('utf-8', errors='replace').splitlines()
                    if lines and lines[-1] != last:
                        try:
                            event = json.loads(lines[-1])
                            progress(event['message'])
                            last = lines[-1]
                        except (ValueError, KeyError):
                            pass
                time.sleep(0.2)
        finally:
            tree.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    result_path = directory / 'result.json'
    if not result_path.is_file() or result_path.stat().st_size > 262144:
        raise RuntimeError('插件没有返回有效结果，详见本机 worker.log。')
    value = json.loads(result_path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('message'), str) or type(value.get('ok')) is not bool or not isinstance(value.get('data', {}), dict):
        raise ValueError('插件返回格式无效')
    if value.get('status') not in ('succeeded','failed','push_pending','uncertain') or (process.returncode and value['ok']):
        raise ValueError('插件返回状态与退出码不一致')
    return value


def worker(path: Path):
    wire = json.loads(path.read_text(encoding='utf-8'))
    directory = Path(wire['job_dir'])
    try:
        plugin_dir = Path(wire['plugin_dir'])
        entry = contained(plugin_dir, wire['entrypoint'])
        sys.path.insert(0, str(plugin_dir))
        spec = importlib.util.spec_from_file_location('local_task_plugin', entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        context = Context(plugin_dir, Path(wire['state_dir']), directory, wire['config'], wire['job_id'])
        value = module.handle(wire['request'], context)
        if len(json.dumps(value).encode('utf-8')) > 262144:
            raise ValueError('插件结果超过 256 KB')
        atomic_json(directory / 'result.json', value)
        return 0
    except BaseException as error:
        with (directory / 'worker-error.log').open('w', encoding='utf-8') as stream:
            stream.write(redact(traceback.format_exc()))
        atomic_json(directory / 'result.json', {'ok': False, 'status': 'failed', 'message': redact(error)[:1000], 'data': {}})
        return 1
