from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid
from .binding import load_config, setup
from .executor import execute, worker
from .locking import FileLock
from .platforms import current
from .plugins import Registry
from .security import load_token
from .storage import Store
from .telegram import Telegram
from .util import Layout, contained, file_hashes, redact, sanitize_filename
from .health import read as read_health


def local_job(layout, command, argument='', allow_execute=False, input_file: Path | None = None):
    registry = Registry(layout).discover()
    if command not in registry.commands:
        raise ValueError('命令未加载：' + command + '; ' + json.dumps(registry.errors, ensure_ascii=False))
    plugin, spec = registry.commands[command]
    if spec['mode'] == 'job' and not allow_execute:
        raise ValueError('此命令会执行实际任务；确认后添加 --execute。')
    input_spec = spec.get('input_files')
    if input_spec and input_spec.get('required', False) and not input_file:
        raise ValueError('此命令需要输入文件，请添加 --file <路径>。')
    if input_file and not input_file.is_file():
        raise ValueError('输入文件不存在：' + str(input_file))
    with FileLock(layout.runtime / 'local-run.lock'):
        store = Store(layout.runtime / 'local-tests.sqlite3')
        try:
            store.recover()
            incoming = input_file.stat().st_size if input_file else 0
            store.prune_jobs_cache(layout.runtime / 'jobs', incoming_bytes=incoming)
            identifier = uuid.uuid4().hex[:16]
            job_dir = registry.snapshot(plugin, identifier)
            resolved_files = []
            if input_file:
                safe_name = sanitize_filename(input_file.name, default='input.bin')
                dest = contained(job_dir / 'inputs', safe_name)
                shutil.copy2(input_file, dest)
                hashes = file_hashes(dest)
                resolved_files.append({
                    'kind': 'document',
                    'name': safe_name,
                    'path': str(dest),
                    'size': hashes['size'],
                    'mime_type': 'application/octet-stream',
                    'md5': hashes['md5'],
                    'sha256': hashes['sha256'],
                })
            job = {'id':identifier, 'update_id':-time.time_ns()//1000, 'plugin':plugin.identifier, 'revision':plugin.revision,
                   'command':command, 'argument':argument, 'mode':spec['mode'], 'permission':spec['permission'],
                   'user_id':0, 'chat_id':0, 'message_id':0,
                   'spec':{**spec, 'config':plugin.config, 'entrypoint':plugin.manifest.get('entrypoint','main.py'),
                           'timeout_seconds':spec.get('timeout_seconds',30 if spec['mode']=='query' else 3600),
                           'resolved_files':resolved_files}}
            store.add_job(job)
            job = store.claim(spec['mode'])
            try:
                value = execute(layout, job, progress=lambda message: print(message, flush=True))
            except Exception as error:
                store.finish(identifier, {'status':'failed','ok':False,'message':str(error),'data':{}})
                raise
            store.finish(identifier, value)
            print(json.dumps({'job_id':identifier, **value}, ensure_ascii=False, indent=2))
            return 0 if value['ok'] else 1
        finally:
            store.close()


def doctor(layout, online=False):
    registry = Registry(layout).discover()
    report = {'python':sys.version.split()[0], 'platform':current(), 'root':str(layout.root),
              'configured':layout.config.is_file(), 'plugins':{k:v.manifest['version'] for k,v in registry.plugins.items()},
              'plugin_errors':registry.errors, 'ignored_directories':['plugins/','runtime/'], 'runtime_health':read_health(layout)}
    if online:
        config = load_config(layout)
        api = Telegram(load_token(config), config.get('proxy'))
        me = api.call('getMe')
        if me['id'] != config['bot_id']:
            raise RuntimeError('Bot 身份与已保存配置不同。')
        report.update(bot='@'+me['username'], chat=config['chat_name'], webhook=bool(api.call('getWebhookInfo').get('url')))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Telegram Task Runner — 本地私有插件任务宿主')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('setup', help='本机隐藏输入 Token，并通过一次性口令绑定聊天')
    command.add_argument('--proxy', help='后台使用的 HTTPS 代理，例如 http://127.0.0.1:7890')
    sub.add_parser('run', help='前台运行宿主')
    check = sub.add_parser('doctor', help='查看本机环境和插件状态')
    check.add_argument('--offline', action='store_true')
    check.add_argument('--online', action='store_true')
    sub.add_parser('plugins', help='查看发现的本地插件')
    service = sub.add_parser('service', help='管理当前平台的自动启动服务')
    service.add_argument('action', choices=('install','start','stop','status','uninstall','export'))
    service.add_argument('--mode', choices=('logon',), default='logon')
    service.add_argument('--account')
    service.add_argument('--output', type=Path)
    local = sub.add_parser('run-plugin', help='本机直接验证一个插件命令，不连接 Telegram')
    local.add_argument('plugin_command')
    local.add_argument('argument', nargs='?', default='')
    local.add_argument('--file', type=Path, dest='input_file', help='传入本地测试文件（模拟 Telegram 附件）')
    local.add_argument('--execute', action='store_true')
    work = sub.add_parser('_worker', help=argparse.SUPPRESS)
    work.add_argument('request', type=Path)
    args = parser.parse_args(argv)
    layout = Layout(args.root.resolve()).ensure()
    try:
        if args.command == '_worker':
            return worker(args.request)
        if args.command == 'doctor':
            return doctor(layout, online=args.online and not args.offline)
        if args.command == 'plugins':
            registry = Registry(layout).discover()
            print(json.dumps({'plugins':{p.identifier:{'version':p.manifest['version'],'commands':p.manifest['commands']} for p in registry.plugins.values()}, 'errors':registry.errors},ensure_ascii=False,indent=2))
            return 0
        if args.command == 'setup':
            with FileLock(layout.runtime / 'host.lock'):
                return setup(layout, args.proxy)
        if args.command == 'run-plugin':
            return local_job(layout, args.plugin_command, args.argument, args.execute, args.input_file)
        if args.command == 'service':
            from .services import manage, arguments, windows_xml
            if args.action == 'export':
                if not args.account or not args.output:
                    raise ValueError('export 需要 --account 和 --output')
                python = Path(sys.executable)
                if python.with_name('pythonw.exe').exists():
                    python = python.with_name('pythonw.exe')
                args.output.write_text(windows_xml(arguments(layout,python),layout.root,args.account,args.mode),encoding='utf-8')
                return 0
            return manage(layout, args.action, args.mode)
        from .host import Host
        host = Host(layout)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: host.stop.set())
        return host.run()
    except (Exception, KeyboardInterrupt) as error:
        text = redact(str(error) or type(error).__name__)
        layout.log(text)
        print('失败：' + text, file=sys.stderr)
        return 1
